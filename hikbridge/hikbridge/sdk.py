"""ctypes binding for the Hikvision HCNetSDK (linux/amd64).

Verified against an EZVIZ/Hikvision OEM PTZ camera (CS-XP1-8B4WF...) which
speaks the Hikvision private protocol on TCP 8000 but exposes no HTTP, ONVIF
or ISAPI service.

Facts established by probing that device:

* login channel for PTZ/voice is 1 (channel 0 and 33 fail)
* only NET_DVR_PTZControl_Other works; the WithSpeed variant returns err 17
* pan/tilt codes 21..24 work, presets and zoom/focus/iris are unsupported
* the SDK's audio-intercom plugin dlopens "./libAudioRender.so" and
  "./libopenal.so.1" relative to the process cwd, so cwd has to be the
  HCNetSDKCom directory while the voice channel is opened
* openal needs an audio device; an ALSA "null" device (see asound.conf) is
  enough, which keeps this usable inside a container with no sound card
* the camera advertises the intercom codec after login.  The OEM camera here
  advertises AAC/16 kHz, and the voice channel rejects raw PCM by playing its
  local "invalid format" beep.  AAC has to be sent as complete ADTS frames.
"""
from __future__ import annotations

import ctypes
import logging
import os
import select
import shutil
import subprocess
import threading
import time
from contextlib import contextmanager

_LOGGER = logging.getLogger("hikbridge")

# Codes 25..28 (diagonals), 29/30 (auto pan) and the zoom/focus/iris block all
# return err 11 on this device, so only the four cardinal moves are exposed.
PTZ_COMMANDS = {
    "up": 21,
    "down": 22,
    "left": 23,
    "right": 24,
}

# NET_DVR_VoiceComSendData accepts small encoded frames.  G.711 is always
# 160 bytes; AAC ADTS frames from ffmpeg are normally under 512 bytes.
VOICE_CHUNK = 1600

# byAudioSamplingRate from NET_DVR_AUDIO_COMPRESS.
_AUDIO_RATE_TABLE = (16000, 16000, 32000, 48000, 44100, 8000)

# The SDK's intercom plugin resolves its sibling libraries through the cwd.
_chdir_lock = threading.Lock()


@contextmanager
def _working_dir(path: str):
    with _chdir_lock:
        previous = os.getcwd()
        os.chdir(path)
        try:
            yield
        finally:
            os.chdir(previous)


class _SDKPath(ctypes.Structure):
    _fields_ = [("sPath", ctypes.c_char * 256), ("byRes", ctypes.c_byte * 128)]


class _JPEGPara(ctypes.Structure):
    _fields_ = [("wPicSize", ctypes.c_uint16), ("wPicQuality", ctypes.c_uint16)]


class _AudioComp(ctypes.Structure):
    _fields_ = [
        ("byAudioEncType", ctypes.c_ubyte),
        ("byAudioSamplingRate", ctypes.c_ubyte),
        ("byAudioBitRate", ctypes.c_ubyte),
        ("byres", ctypes.c_ubyte * 4),
        ("bySupport", ctypes.c_ubyte),
    ]


_VOICE_CB = ctypes.CFUNCTYPE(
    None, ctypes.c_int32, ctypes.c_char_p, ctypes.c_uint32, ctypes.c_ubyte, ctypes.c_void_p
)


class HikError(RuntimeError):
    pass


class _VoiceEncoder:
    """Encode PCM16 mono into the codec announced by the camera.

    HCNetSDK's MR voice channel wants encoded frames, not PCM.  ffmpeg is the
    smallest reliable way to handle both AAC ADTS and G.711 without pulling a
    large audio dependency into the add-on.
    """

    def __init__(
        self,
        codec: str,
        target_rate: int,
        input_rate: int,
        ffmpeg: str = "ffmpeg",
    ) -> None:
        self.codec = codec
        self.target_rate = target_rate
        self.input_rate = input_rate
        self._buffer = b""
        self._eof = False
        self._input_closed = False
        self._proc: subprocess.Popen | None = None

        if codec == "aac":
            output = [
                "-c:a", "aac",
                "-b:a", "32k",
                "-ar", str(target_rate),
                "-ac", "1",
                "-f", "adts",
            ]
        elif codec == "g711u":
            output = [
                "-c:a", "pcm_mulaw",
                "-ar", "8000",
                "-ac", "1",
                "-f", "mulaw",
            ]
        elif codec == "g711a":
            output = [
                "-c:a", "pcm_alaw",
                "-ar", "8000",
                "-ac", "1",
                "-f", "alaw",
            ]
        else:
            raise HikError(f"unsupported voice codec {codec!r}")

        if not shutil.which(ffmpeg):
            raise HikError(f"ffmpeg is not installed ({ffmpeg})")

        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel", "error",
            "-f", "s16le",
            "-ar", str(input_rate),
            "-ac", "1",
            "-i", "pipe:0",
            *output,
            "-flush_packets", "1",
            "pipe:1",
        ]
        self._proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    @property
    def eof(self) -> bool:
        return self._eof

    def write(self, pcm: bytes) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise HikError("voice encoder is not running")
        if self._input_closed:
            raise HikError("voice encoder input is already closed")
        # PCM16 samples are two bytes; an odd trailing byte is not a sample.
        pcm = pcm[: len(pcm) // 2 * 2]
        if not pcm:
            return
        try:
            self._proc.stdin.write(pcm)
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise HikError(f"voice encoder stopped: {exc}") from exc

    def close_input(self) -> None:
        if self._input_closed:
            return
        self._input_closed = True
        if self._proc is not None and self._proc.stdin is not None:
            try:
                self._proc.stdin.close()
            except OSError:
                pass

    def read_frames(self, timeout: float = 0.0) -> list[bytes]:
        """Return complete encoded frames currently available."""
        if self._proc is None or self._proc.stdout is None:
            return []

        if timeout > 0:
            try:
                ready, _, _ = select.select([self._proc.stdout], [], [], timeout)
            except (OSError, ValueError):
                ready = []
        else:
            ready = [self._proc.stdout]

        if ready:
            try:
                data = os.read(self._proc.stdout.fileno(), 8192)
            except (BlockingIOError, OSError):
                data = b""
            if data:
                self._buffer += data
            else:
                self._eof = True

        return self._split_frames()

    def _split_frames(self) -> list[bytes]:
        frames: list[bytes] = []
        if self.codec == "aac":
            while len(self._buffer) >= 7:
                if self._buffer[0] != 0xFF or self._buffer[1] & 0xF0 != 0xF0:
                    sync = self._buffer.find(b"\xff", 1)
                    if sync < 0:
                        self._buffer = b""
                        break
                    self._buffer = self._buffer[sync:]
                    continue
                length = (
                    ((self._buffer[3] & 0x03) << 11)
                    | (self._buffer[4] << 3)
                    | (self._buffer[5] >> 5)
                )
                if length < 7:
                    self._buffer = self._buffer[1:]
                    continue
                if len(self._buffer) < length:
                    break
                frames.append(self._buffer[:length])
                self._buffer = self._buffer[length:]
        else:
            # G.711 is a continuous 8 kHz byte stream: one frame is 20 ms.
            while len(self._buffer) >= 160:
                frames.append(self._buffer[:160])
                self._buffer = self._buffer[160:]
        return frames

    def wait(self, timeout: float = 2.0) -> None:
        if self._proc is None:
            return
        try:
            self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait()

    def close(self) -> None:
        if self._proc is None:
            return
        self.close_input()
        self.wait()


class HikCamera:
    """A single, long-lived HCNetSDK session.

    The device tolerates exactly one control session; opening a second one
    while the first is alive makes PTZ calls fail with err 11. Every public
    method therefore takes the same re-entrant lock and the session is never
    closed between commands.
    """

    def __init__(
        self,
        ip: str,
        port: int = 8000,
        username: str = "admin",
        password: str = "",
        channel: int = 1,
        sdk_dir: str | None = None,
        speak_sample_rate: int = 16000,
    ) -> None:
        self.ip = ip
        self.port = port
        self.username = username
        self.password = password
        self.channel = channel
        self.speak_sample_rate = speak_sample_rate
        self.audio_rate = speak_sample_rate
        self.audio_codec: str | None = None
        self.audio_enc_type: int | None = None
        self.sdk_dir = sdk_dir or os.path.join(os.path.dirname(__file__), "hcnetsdk")
        self.com_dir = os.path.join(self.sdk_dir, "HCNetSDKCom")
        self.uid = -1
        self.serial = ""
        self._lock = threading.RLock()
        self._voice_handle = -1
        self._voice_cb = _VOICE_CB(self._on_voice_data)
        self._encoder: _VoiceEncoder | None = None
        self._received = 0
        self._sdk = None
        self._load()

    # -- library loading ---------------------------------------------------
    def _load(self) -> None:
        # openal reads this when the intercom plugin is first used; pointing it
        # at our own config avoids needing write access to /etc.
        os.environ.setdefault(
            "ALSA_CONFIG_PATH", os.path.join(os.path.dirname(__file__), "asound.conf")
        )
        lib = os.path.join(self.sdk_dir, "libhcnetsdk.so")
        if not os.path.exists(lib):
            raise HikError(f"HCNetSDK not found at {lib}")
        self._sdk = ctypes.CDLL(lib, mode=ctypes.RTLD_GLOBAL)
        self._bind()
        cfg = _SDKPath()
        cfg.sPath = self.sdk_dir.encode()
        self._sdk.NET_DVR_SetSDKInitCfg(2, ctypes.byref(cfg))

    def _bind(self) -> None:
        s = self._sdk
        s.NET_DVR_Init.restype = ctypes.c_bool
        s.NET_DVR_Init.argtypes = []
        s.NET_DVR_Cleanup.restype = ctypes.c_bool
        s.NET_DVR_Cleanup.argtypes = []
        s.NET_DVR_GetLastError.restype = ctypes.c_uint32
        s.NET_DVR_GetLastError.argtypes = []
        s.NET_DVR_SetConnectTime.restype = ctypes.c_bool
        s.NET_DVR_SetConnectTime.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
        s.NET_DVR_SetReconnect.restype = ctypes.c_bool
        s.NET_DVR_SetReconnect.argtypes = [ctypes.c_uint32, ctypes.c_bool]
        s.NET_DVR_Login_V30.restype = ctypes.c_int32
        s.NET_DVR_Login_V30.argtypes = [
            ctypes.c_char_p, ctypes.c_uint16, ctypes.c_char_p, ctypes.c_char_p,
            ctypes.c_void_p,
        ]
        s.NET_DVR_Logout.restype = ctypes.c_bool
        s.NET_DVR_Logout.argtypes = [ctypes.c_int32]
        s.NET_DVR_PTZControl_Other.restype = ctypes.c_bool
        s.NET_DVR_PTZControl_Other.argtypes = [
            ctypes.c_int32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
        ]
        s.NET_DVR_CaptureJPEGPicture.restype = ctypes.c_bool
        s.NET_DVR_CaptureJPEGPicture.argtypes = [
            ctypes.c_int32, ctypes.c_int32, ctypes.POINTER(_JPEGPara), ctypes.c_char_p,
        ]

        # Newer SDKs expose the codec query and the MR voice API.  The MR API
        # is the one that accepts encoded intercom frames; the old V30 API is
        # kept only as a compatibility fallback.
        self._get_audio_compress = getattr(
            s, "NET_DVR_GetCurrentAudioCompress", None
        )
        if self._get_audio_compress is not None:
            self._get_audio_compress.restype = ctypes.c_bool
            self._get_audio_compress.argtypes = [
                ctypes.c_int32, ctypes.POINTER(_AudioComp)
            ]
        self._start_voice_mr = getattr(s, "NET_DVR_StartVoiceCom_MR_V30", None)
        if self._start_voice_mr is not None:
            self._start_voice_mr.restype = ctypes.c_int32
            self._start_voice_mr.argtypes = [
                ctypes.c_int32, ctypes.c_uint32, _VOICE_CB, ctypes.c_void_p
            ]
        self._start_voice_legacy = getattr(s, "NET_DVR_StartVoiceCom_V30", None)
        if self._start_voice_legacy is not None:
            self._start_voice_legacy.restype = ctypes.c_int32
            self._start_voice_legacy.argtypes = [
                ctypes.c_int32, ctypes.c_uint32, ctypes.c_bool, _VOICE_CB,
                ctypes.c_void_p,
            ]

        s.NET_DVR_StopVoiceCom.restype = ctypes.c_bool
        s.NET_DVR_StopVoiceCom.argtypes = [ctypes.c_int32]
        s.NET_DVR_VoiceComSendData.restype = ctypes.c_bool
        s.NET_DVR_VoiceComSendData.argtypes = [
            ctypes.c_int32, ctypes.c_char_p, ctypes.c_uint32
        ]

    # -- session -----------------------------------------------------------
    def _on_voice_data(self, handle, buf, size, flag, user) -> None:
        self._received += size

    def _read_audio_compression(self) -> None:
        """Read the camera's advertised intercom codec after login."""
        if self._get_audio_compress is None:
            _LOGGER.warning(
                "camera %s: SDK has no NET_DVR_GetCurrentAudioCompress; "
                "speaker playback cannot select the right codec", self.ip
            )
            return
        info = _AudioComp()
        if not self._get_audio_compress(self.uid, ctypes.byref(info)):
            _LOGGER.warning(
                "camera %s: cannot query intercom codec (SDK error %s)",
                self.ip, self.last_error(),
            )
            return

        enc = int(info.byAudioEncType)
        rate_index = int(info.byAudioSamplingRate)
        rate = _AUDIO_RATE_TABLE[rate_index] if rate_index < len(_AUDIO_RATE_TABLE) else 16000
        codec = {
            1: "g711u",
            2: "g711a",
            7: "aac",
            12: "aac",
        }.get(enc)
        if codec is None:
            _LOGGER.warning(
                "camera %s: unsupported intercom codec %s (sample-rate index %s)",
                self.ip, enc, rate_index,
            )
            return
        if codec != "aac":
            # G.711 voice intercom is always the 8 kHz, 20 ms format.
            rate = 8000
        self.audio_enc_type = enc
        self.audio_codec = codec
        self.audio_rate = rate
        _LOGGER.info(
            "camera %s: intercom codec=%s rate=%s Hz",
            self.ip, codec, rate,
        )

    def login(self) -> None:
        with self._lock:
            if self.uid >= 0:
                return
            if not self._sdk.NET_DVR_Init():
                raise HikError(f"NET_DVR_Init failed ({self.last_error()})")
            self._sdk.NET_DVR_SetConnectTime(5000, 1)
            self._sdk.NET_DVR_SetReconnect(5000, True)
            info = ctypes.create_string_buffer(2048)
            last = None
            for _ in range(3):
                self.uid = self._sdk.NET_DVR_Login_V30(
                    self.ip.encode(), self.port, self.username.encode(),
                    self.password.encode(), ctypes.cast(info, ctypes.c_void_p),
                )
                if self.uid >= 0:
                    break
                last = self.last_error()
                time.sleep(0.5)
            if self.uid < 0:
                raise HikError(f"login failed ({last})")
            raw = info.raw
            self.serial = raw[:48].split(b"\x00")[0].decode("latin1", "replace")
            self._device = {
                "serial": self.serial,
                "byDVRType": raw[52],
                "byChanNum": raw[53],
                "byStartChan": raw[54],
                "byZeroChanNum": raw[57],
            }
            self._read_audio_compression()

    def close(self) -> None:
        with self._lock:
            self.talk_stop()
            if self.uid >= 0:
                self._sdk.NET_DVR_Logout(self.uid)
                self.uid = -1
            self._sdk.NET_DVR_Cleanup()

    def last_error(self) -> str:
        return str(self._sdk.NET_DVR_GetLastError())

    @property
    def device(self) -> dict:
        return dict(getattr(self, "_device", {}))

    # -- PTZ ---------------------------------------------------------------
    def ptz(self, direction: str, stop: bool = False) -> bool:
        if direction not in PTZ_COMMANDS:
            raise HikError(f"unknown direction {direction!r}")
        with self._lock:
            return bool(
                self._sdk.NET_DVR_PTZControl_Other(
                    self.uid, self.channel, PTZ_COMMANDS[direction], 1 if stop else 0
                )
            )

    def stop(self) -> bool:
        return self.ptz("up", stop=True)

    def nudge(self, direction: str, seconds: float = 0.6) -> None:
        seconds = max(0.05, min(float(seconds), 10.0))
        with self._lock:
            if not self.ptz(direction, False):
                raise HikError(f"PTZ {direction} start failed ({self.last_error()})")
            try:
                time.sleep(seconds)
            finally:
                self.ptz(direction, True)

    # -- snapshot ----------------------------------------------------------
    def snapshot(self, path: str, size: int = 2, quality: int = 0) -> str:
        para = _JPEGPara(size, quality)
        with self._lock:
            if not self._sdk.NET_DVR_CaptureJPEGPicture(
                self.uid, self.channel, ctypes.byref(para), path.encode()
            ):
                raise HikError(f"snapshot failed ({self.last_error()})")
        return path

    # -- voice (TTS to the camera speaker, and device mic back to us) -------
    def _ensure_encoder(self, input_rate: int) -> None:
        if self.audio_codec is None:
            # A camera may have come online before the codec query succeeded.
            self._read_audio_compression()
        if self.audio_codec is None:
            raise HikError(
                "camera did not report a supported intercom codec; "
                "speaker playback is unavailable"
            )
        if self._encoder is not None and self._encoder.input_rate == input_rate:
            return
        self._close_encoder()
        self._encoder = _VoiceEncoder(
            self.audio_codec, self.audio_rate, input_rate
        )

    def _send_encoded(self, frames: list[bytes]) -> None:
        if self._voice_handle < 0:
            raise HikError("voice channel is not open")
        for frame in frames:
            if not frame:
                continue
            if len(frame) > VOICE_CHUNK:
                # This should not happen for AAC ADTS or 20 ms G.711, but
                # never corrupt a codec frame by blindly splitting it.
                raise HikError(
                    f"encoded voice frame is too large ({len(frame)} bytes)"
                )
            if not self._sdk.NET_DVR_VoiceComSendData(
                self._voice_handle, frame, len(frame)
            ):
                raise HikError(
                    f"VoiceComSendData failed ({self.last_error()})"
                )

    def _read_encoded(self, timeout: float = 0.0) -> None:
        if self._encoder is not None:
            self._send_encoded(self._encoder.read_frames(timeout))

    def _finish_encoder(self) -> None:
        """Flush the encoder, sending every complete frame before closing."""
        encoder = self._encoder
        if encoder is None:
            return
        encoder.close_input()
        deadline = time.monotonic() + 3.0
        while True:
            frames = encoder.read_frames(0.05)
            if frames:
                self._send_encoded(frames)
                continue
            if encoder.eof:
                break
            if time.monotonic() >= deadline:
                break
        encoder.wait()
        self._encoder = None

    def _close_encoder(self) -> None:
        encoder, self._encoder = self._encoder, None
        if encoder is not None:
            encoder.close()

    def talk_start(self) -> int:
        with self._lock:
            if self._voice_handle >= 0:
                return self._voice_handle
            # The MR variant accepts encoded frames and is the API used by
            # Hikvision's own talk clients.  bNeedAudio is not a parameter
            # here: the callback still receives the camera microphone stream.
            with _working_dir(self.com_dir):
                if self._start_voice_mr is not None:
                    self._voice_handle = self._start_voice_mr(
                        self.uid, self.channel, self._voice_cb, None
                    )
                elif self._start_voice_legacy is not None:
                    self._voice_handle = self._start_voice_legacy(
                        self.uid, self.channel, False, self._voice_cb, None
                    )
                else:
                    raise HikError("SDK has no voice-com API")
            if self._voice_handle < 0:
                raise HikError(f"StartVoiceCom failed ({self.last_error()})")
            return self._voice_handle

    def talk_send(self, pcm: bytes, rate: int | None = None) -> int:
        """Encode PCM16 mono and push it to the camera speaker.

        ``pcm`` is the format used by the browser/TTS caller.  ``rate`` is its
        sample rate; the camera's advertised rate and codec are handled here.
        """
        if not pcm:
            return 0
        rate = int(rate or self.audio_rate or self.speak_sample_rate)
        if rate <= 0:
            raise HikError(f"invalid PCM sample rate {rate!r}")
        with self._lock:
            if self._voice_handle < 0:
                raise HikError("voice channel is not open")
            self._ensure_encoder(rate)
            assert self._encoder is not None
            self._encoder.write(pcm)
            # Read whatever ffmpeg has produced already.  This keeps a long
            # TTS clip from filling the encoder stdout pipe while we pace it.
            self._read_encoded(0.01)
            sent = len(pcm)
            # Pace the caller so the camera receives roughly real-time audio.
            time.sleep(sent / 2.0 / rate)
            return sent

    def talk_stop(self) -> None:
        with self._lock:
            try:
                self._finish_encoder()
            except HikError as exc:
                _LOGGER.warning("could not flush voice encoder: %s", exc)
                self._close_encoder()
            if self._voice_handle >= 0:
                self._sdk.NET_DVR_StopVoiceCom(self._voice_handle)
                self._voice_handle = -1

    def speak(self, pcm: bytes, rate: int | None = None) -> int:
        """Push 16-bit mono PCM to the camera speaker."""
        if not pcm:
            return 0
        rate = int(rate or self.audio_rate or self.speak_sample_rate)
        # Feed in 100 ms chunks so ffmpeg's stdout cannot fill while the
        # caller is sleeping for real-time pacing.
        chunk_samples = max(1, int(rate * 0.1))
        chunk_bytes = chunk_samples * 2
        with self._lock:
            self.talk_start()
            try:
                sent = 0
                for offset in range(0, len(pcm), chunk_bytes):
                    sent += self.talk_send(pcm[offset:offset + chunk_bytes], rate)
                return sent
            finally:
                self.talk_stop()
