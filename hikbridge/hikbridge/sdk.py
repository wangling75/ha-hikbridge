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
"""
from __future__ import annotations

import ctypes
import os
import threading
import time
from contextlib import contextmanager

# Codes 25..28 (diagonals), 29/30 (auto pan) and the zoom/focus/iris block all
# return err 11 on this device, so only the four cardinal moves are exposed.
PTZ_COMMANDS = {
    "up": 21,
    "down": 22,
    "left": 23,
    "right": 24,
}

# Maximum chunk accepted by NET_DVR_VoiceComSendData (8000 was rejected).
VOICE_CHUNK = 1600

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


_VOICE_CB = ctypes.CFUNCTYPE(
    None, ctypes.c_int32, ctypes.c_char_p, ctypes.c_uint32, ctypes.c_ubyte, ctypes.c_void_p
)


class HikError(RuntimeError):
    pass


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
        self.sdk_dir = sdk_dir or os.path.join(os.path.dirname(__file__), "hcnetsdk")
        self.com_dir = os.path.join(self.sdk_dir, "HCNetSDKCom")
        self.uid = -1
        self.serial = ""
        self._lock = threading.RLock()
        self._voice_handle = -1
        self._voice_cb = _VOICE_CB(self._on_voice_data)
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
        s.NET_DVR_StartVoiceCom_V30.restype = ctypes.c_int32
        s.NET_DVR_StartVoiceCom_V30.argtypes = [
            ctypes.c_int32, ctypes.c_uint32, ctypes.c_bool, _VOICE_CB, ctypes.c_void_p,
        ]
        s.NET_DVR_StopVoiceCom.restype = ctypes.c_bool
        s.NET_DVR_StopVoiceCom.argtypes = [ctypes.c_int32]
        s.NET_DVR_VoiceComSendData.restype = ctypes.c_bool
        s.NET_DVR_VoiceComSendData.argtypes = [ctypes.c_int32, ctypes.c_char_p, ctypes.c_uint32]

    # -- session -----------------------------------------------------------
    def _on_voice_data(self, handle, buf, size, flag, user) -> None:
        self._received += size

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
    def talk_start(self) -> int:
        with self._lock:
            if self._voice_handle >= 0:
                return self._voice_handle
            # bNeedAudio=False: we push audio ourselves instead of letting the
            # SDK capture from a local microphone.
            with _working_dir(self.com_dir):
                self._voice_handle = self._sdk.NET_DVR_StartVoiceCom_V30(
                    self.uid, self.channel, False, self._voice_cb, None
                )
            if self._voice_handle < 0:
                raise HikError(f"StartVoiceCom failed ({self.last_error()})")
            return self._voice_handle

    def talk_send(self, pcm: bytes, rate: int | None = None) -> int:
        with self._lock:
            if self._voice_handle < 0:
                raise HikError("voice channel is not open")
            rate = rate or self.speak_sample_rate
            sent = 0
            for offset in range(0, len(pcm), VOICE_CHUNK):
                chunk = pcm[offset:offset + VOICE_CHUNK]
                if not self._sdk.NET_DVR_VoiceComSendData(
                    self._voice_handle, chunk, len(chunk)
                ):
                    raise HikError(
                        f"VoiceComSendData failed at {offset} ({self.last_error()})"
                    )
                sent += len(chunk)
                # pace the stream so the device buffer does not overflow
                time.sleep(len(chunk) / 2.0 / rate)
            return sent

    def talk_stop(self) -> None:
        with self._lock:
            if self._voice_handle >= 0:
                self._sdk.NET_DVR_StopVoiceCom(self._voice_handle)
                self._voice_handle = -1

    def speak(self, pcm: bytes, rate: int | None = None) -> int:
        """Push 16-bit mono PCM to the camera speaker."""
        with self._lock:
            self.talk_start()
            try:
                return self.talk_send(pcm, rate)
            finally:
                self.talk_stop()
