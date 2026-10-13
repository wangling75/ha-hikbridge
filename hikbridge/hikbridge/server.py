"""Tiny stdlib HTTP API in front of one or more HikCamera sessions.

Every camera is addressed by its own id, so a single bridge process can drive
several cameras at once:

  GET  /                                 -> built-in web UI (ui.html)
  GET  /health                           -> {"ok": true, "cameras": [...]}
  GET  /api/cameras                      -> {"ok": true, "cameras": [...]}
  GET  /api/cameras/<id>/snapshot.jpg    -> image/jpeg (?download=1 saves it)
  GET  /api/cameras/<id>/live.mp4        -> fragmented MP4 (video + AAC) for the web UI
  POST /api/cameras/<id>/ptz             {"direction": "right", "duration": 0.6}
  POST /api/cameras/<id>/ptz/start       {"direction": "right"}
  POST /api/cameras/<id>/ptz/stop
  POST /api/cameras/<id>/speak           -> body is a WAV file
  POST /api/cameras/<id>/speak_raw?rate= -> body is PCM16 mono; encoded for the camera
  GET  /api/cameras/<id>/talk            -> WebSocket, live two-way audio

The original single-camera paths (/ptz, /ptz/stop, /snapshot.jpg, /speak,
/speak_raw) are kept as aliases for the first configured camera, so an older
Home Assistant integration keeps working without any change.

When an auth token is configured it is accepted either through the
X-Auth-Token header or a ?token= query parameter (the latter is needed for
<img> tags and browser WebSockets, which cannot set custom headers).
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time

import hikbridge  # absolute self-import: reads the live timezone status
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import __version__
from .audio import prepare_wav
from .sdk import PTZ_COMMANDS, HikCamera, HikError
from .websocket import OP_BINARY, OP_TEXT, WebSocket, WebSocketError

_LOGGER = logging.getLogger("hikbridge")

# Largest single PCM frame accepted on the talk socket (browser sends ~2-4 kB).
_MAX_TALK_FRAME = 64 * 1024
# Drop an idle talk session so the camera voice channel is never held forever.
_TALK_IDLE_TIMEOUT = 60.0

_UI_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui.html")
_ui_cache: bytes | None = None
_ui_lock = threading.Lock()


def _ui_page() -> bytes:
    global _ui_cache
    with _ui_lock:
        if _ui_cache is None:
            try:
                with open(_UI_PATH, "rb") as fh:
                    _ui_cache = fh.read()
            except OSError as exc:  # pragma: no cover - packaging error
                _LOGGER.error("cannot read web UI at %s: %s", _UI_PATH, exc)
                _ui_cache = (
                    b"<!doctype html><meta charset=utf-8>"
                    b"<h1>hikbridge</h1><p>web UI file is missing</p>"
                )
        return _ui_cache


@dataclass
class CameraEntry:
    """One camera plus the state that belongs to it."""

    id: str
    name: str
    camera: HikCamera
    rtsp_url: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    talking: bool = False

    def info(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "serial": self.camera.serial,
            "device": self.camera.device,
            "audio_codec": self.camera.audio_codec,
            "audio_rate": self.camera.audio_rate,
            "live": bool(self.rtsp_url),
        }


class _State:
    def __init__(self, cameras: list[CameraEntry], token: str | None = None,
                 https_port: int | None = None) -> None:
        if not cameras:
            raise ValueError("at least one camera is required")
        self.cameras = list(cameras)
        self.by_id = {entry.id: entry for entry in self.cameras}
        self.token = token or None
        # Advertised through /health so the web UI can point the user at the
        # HTTPS origin when the microphone is blocked on plain HTTP.
        self.https_port = https_port

    @property
    def default(self) -> CameraEntry:
        return self.cameras[0]

    def info_list(self) -> list[dict]:
        return [entry.info() for entry in self.cameras]


def _split(path: str) -> list[str]:
    return [unquote(part) for part in path.strip("/").split("/") if part]


def _normalise_path(path: str) -> str:
    """Drop a Home Assistant ingress prefix so one handler serves both.

    The add-on is reachable directly on http://<host>:8654/ and through the
    HA sidebar panel at /api/hassio_ingress/<token>/. Supervisor normally
    strips that prefix before proxying, but strip it here too so both paths
    behave identically.
    """
    prefix = "/api/hassio_ingress/"
    if path.startswith(prefix):
        _, _, tail = path[len(prefix):].partition("/")
        return "/" + tail
    return path


def _handler(state: _State):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"hikbridge/{__version__}"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # noqa: A003
            _LOGGER.debug("%s - %s", self.address_string(), fmt % args)

        # -- helpers -------------------------------------------------------
        def _send(self, code: int, body: bytes, content_type: str,
                  extra: dict[str, str] | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, payload: dict) -> None:
            self._send(code, json.dumps(payload, ensure_ascii=False).encode(),
                       "application/json; charset=utf-8")

        def _authorised(self, query: dict | None = None) -> bool:
            if not state.token:
                return True
            supplied = self.headers.get("X-Auth-Token") or ""
            if not supplied and query:
                supplied = (query.get("token") or [""])[0]
            if supplied == state.token:
                return True
            self._json(401, {"ok": False, "error": "unauthorised"})
            return False

        def _body(self) -> bytes:
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(length) if length else b""

        def _entry_for(self, parts: list[str]) -> tuple[CameraEntry | None, str]:
            """Resolve /api/cameras/<id>/<action> or fall back to camera #1."""
            if len(parts) >= 4 and parts[0] == "api" and parts[1] == "cameras":
                return state.by_id.get(parts[2]), "/".join(parts[3:])
            return state.default, "/".join(parts)

        # -- routes --------------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = _normalise_path(parsed.path).rstrip("/") or "/"
            query = parse_qs(parsed.query)

            if path in ("/", "/index.html", "/ui.html", "/ui"):
                self._send(200, _ui_page(), "text/html; charset=utf-8",
                           {"Cache-Control": "no-store"})
                return

            if path == "/api/auth":
                self._json(200, {"ok": True, "required": bool(state.token)})
                return

            if not self._authorised(query):
                return

            parts = _split(path)
            try:
                if path == "/health":
                    default = state.default
                    self._json(200, {
                        "ok": True,
                        "version": __version__,
                        "timezone": os.environ.get("TZ") or time.tzname[0],
                        "timezone_status": hikbridge.timezone_status,
                        "https_port": state.https_port,
                        "serial": default.camera.serial,
                        "device": default.camera.device,
                        "cameras": state.info_list(),
                    })
                    return
                if path == "/api/cameras":
                    self._json(200, {"ok": True, "cameras": state.info_list()})
                    return

                entry, action = self._entry_for(parts)
                if entry is None:
                    self._json(404, {"ok": False, "error": "unknown camera"})
                    return
                if action in ("snapshot", "snapshot.jpg"):
                    self._snapshot(entry, query)
                    return
                if action in ("live", "live.mp4"):
                    self._live(entry)
                    return
                if action == "talk":
                    self._talk(entry)
                    return
                self._json(404, {"ok": False, "error": "not found"})
            except HikError as exc:
                self._json(502, {"ok": False, "error": str(exc)})

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = _normalise_path(parsed.path).rstrip("/") or "/"
            query = parse_qs(parsed.query)
            body = self._body()
            if path == "/api/login":
                self._login(body)
                return
            if not self._authorised(query):
                return
            parts = _split(path)
            entry, action = self._entry_for(parts)
            if entry is None:
                self._json(404, {"ok": False, "error": "unknown camera"})
                return
            try:
                if action in ("ptz", "ptz/start"):
                    self._ready(entry)
                    payload = json.loads(body or b"{}")
                    direction = str(payload.get("direction", ""))
                    if direction not in PTZ_COMMANDS:
                        self._json(400, {"ok": False, "error": "bad direction"})
                        return
                    if action == "ptz/start":
                        with entry.lock:
                            ok = entry.camera.ptz(direction, False)
                        self._json(200, {"ok": bool(ok), "direction": direction})
                        return
                    duration = float(payload.get("duration", 0.6))
                    with entry.lock:
                        entry.camera.nudge(direction, duration)
                    self._json(200, {
                        "ok": True, "camera": entry.id,
                        "direction": direction, "duration": duration,
                    })
                elif action == "ptz/stop":
                    self._ready(entry)
                    with entry.lock:
                        ok = entry.camera.stop()
                    self._json(200, {"ok": bool(ok), "camera": entry.id})
                elif action in ("speak", "speak_raw"):
                    self._ready(entry)
                    if not body:
                        self._json(400, {"ok": False, "error": "empty body"})
                        return
                    if entry.talking:
                        self._json(409, {
                            "ok": False,
                            "error": "live talk session is using this camera",
                        })
                        return
                    if action == "speak":
                        speak_rate = entry.camera.audio_rate or entry.camera.speak_sample_rate
                        pcm = prepare_wav(body, speak_rate)
                    else:
                        pcm = body
                    # speak_raw is also PCM16; HikCamera encodes it to the
                    # codec advertised by the camera before sending it.
                    rate = int(query.get("rate", [entry.camera.audio_rate or entry.camera.speak_sample_rate])[0])
                    with entry.lock:
                        sent = entry.camera.speak(pcm, rate)
                    self._json(200, {"ok": True, "camera": entry.id, "bytes": sent})
                else:
                    self._json(404, {"ok": False, "error": "not found"})
            except HikError as exc:
                self._json(502, {"ok": False, "error": str(exc)})
            except (ValueError, OSError) as exc:
                self._json(400, {"ok": False, "error": str(exc)})

        # -- individual endpoints -----------------------------------------
        def _ready(self, entry: CameraEntry) -> None:
            """Reconnect a camera that was offline when the add-on started."""
            camera = entry.camera
            if getattr(camera, "uid", 0) >= 0:
                return
            camera.login()

        def _login(self, body: bytes) -> None:
            """Password check used by the web UI login form."""
            if not state.token:
                self._json(200, {"ok": True, "required": False})
                return
            try:
                payload = json.loads(body or b"{}")
            except ValueError:
                payload = {}
            supplied = str(payload.get("password") or "")
            if supplied and secrets.compare_digest(supplied, state.token):
                self._json(200, {"ok": True, "required": True})
                return
            time.sleep(0.4)  # slow down guessing
            self._json(401, {"ok": False, "error": "wrong password"})

        def _snapshot(self, entry: CameraEntry, query: dict) -> None:
            self._ready(entry)
            with entry.lock:
                with tempfile.TemporaryDirectory() as tmp:
                    target = os.path.join(tmp, "snap.jpg")
                    entry.camera.snapshot(target)
                    with open(target, "rb") as fh:
                        data = fh.read()
            download = (query.get("download") or ["0"])[0] not in ("", "0", "false")
            extra = {"Cache-Control": "no-store"}
            if download:
                stamp = time.strftime("%Y%m%d-%H%M%S")
                extra["Content-Disposition"] = (
                    f'attachment; filename="{entry.id}-{stamp}.jpg"'
                )
            self._send(200, data, "image/jpeg", extra)

        def _live(self, entry: CameraEntry) -> None:
            """Stream RTSP to the browser as fragmented MP4.

            The browser cannot play RTSP directly. ffmpeg scales the video to
            720p and converts any camera audio to AAC, which MSE can append as
            it arrives. The HTTP response is chunked so playback starts before
            the live stream has finished.
            """
            if not entry.rtsp_url:
                self._json(409, {"ok": False, "error": "camera has no rtsp_url configured"})
                return
            if not shutil.which("ffmpeg"):
                self._json(501, {"ok": False, "error": "ffmpeg is not installed"})
                return

            # Some Hikvision cameras send absolute RTP timestamps for audio.
            # Reset both streams to zero so fragmented MP4 stays in range and
            # the browser keeps the audio track alive.
            command = [
                "ffmpeg", "-hide_banner", "-loglevel", "warning",
                "-rtsp_transport", "tcp", "-i", entry.rtsp_url,
                "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn",
                "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                "-pix_fmt", "yuv420p", "-s", "1280x720",
                "-r", "22", "-g", "22", "-keyint_min", "22",
                "-sc_threshold", "0",
                "-b:v", "1800k", "-maxrate", "2200k", "-bufsize", "3600k",
                "-c:a", "aac", "-b:a", "96k",
                "-ar", "48000", "-ac", "1",
                "-af",
                "asetpts=PTS-STARTPTS,aresample=async=1:first_pts=0:min_hard_comp=0.1",
                "-vsync", "cfr", "-avoid_negative_ts", "make_zero",
                "-movflags", "frag_keyframe+empty_moov+default_base_moof",
                "-flush_packets", "1", "-f", "mp4", "pipe:1",
            ]
            proc = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=0,
            )
            if proc.stderr is not None:
                def _drain_stderr() -> None:
                    for line in iter(proc.stderr.readline, b""):
                        text = line.decode("utf-8", "replace").strip()
                        if text:
                            _LOGGER.debug("live ffmpeg: %s", text)
                threading.Thread(
                    target=_drain_stderr, name="hikbridge-live-ffmpeg", daemon=True
                ).start()
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                assert proc.stdout is not None
                while True:
                    chunk = os.read(proc.stdout.fileno(), 16384)
                    if not chunk:
                        break
                    self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
                    self.wfile.write(chunk)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                except (OSError, subprocess.TimeoutExpired):
                    proc.kill()
                try:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except OSError:
                    pass

        def _talk(self, entry: CameraEntry) -> None:
            """Live push-to-talk: browser streams PCM16 mono over a WebSocket."""
            ws = WebSocket(self)
            try:
                ws.handshake()
            except WebSocketError as exc:
                _LOGGER.debug("talk handshake failed: %s", exc)
                return

            # Interactive audio consists of many small packets; disabling
            # Nagle avoids adding tens of milliseconds at the start of talk.
            try:
                self.connection.setsockopt(
                    socket.IPPROTO_TCP, socket.TCP_NODELAY, 1
                )
            except OSError:
                pass

            with entry.lock:
                busy = entry.talking
                if not busy:
                    entry.talking = True
            if busy:
                try:
                    ws.send_text(json.dumps({"ok": False, "error": "busy"}))
                except Exception:  # noqa: BLE001 - peer may be gone already
                    pass
                ws.close()
                return

            try:
                self.connection.settimeout(_TALK_IDLE_TIMEOUT)
            except OSError:
                pass

            rate = entry.camera.audio_rate or entry.camera.speak_sample_rate
            frames = 0
            try:
                self._ready(entry)
                entry.camera.talk_start()
                ws.send_text(json.dumps({"ok": True, "camera": entry.id, "rate": rate, "codec": entry.camera.audio_codec}))
                while True:
                    opcode, payload = ws.recv()
                    if opcode == OP_TEXT:
                        try:
                            message = json.loads(payload.decode("utf-8"))
                        except (ValueError, UnicodeDecodeError):
                            continue
                        if str(message.get("cmd", "")).lower() in ("stop", "close", "end"):
                            break
                        if message.get("rate"):
                            try:
                                rate = int(message["rate"])
                            except (TypeError, ValueError):
                                pass
                        continue
                    if opcode != OP_BINARY or not payload:
                        continue
                    if len(payload) > _MAX_TALK_FRAME:
                        _LOGGER.debug("dropping oversized talk frame (%d bytes)", len(payload))
                        continue
                    entry.camera.talk_send(
                        payload, rate, pace=False, read_timeout=0.0
                    )
                    frames += 1
            except (WebSocketError, HikError, OSError) as exc:
                _LOGGER.info("talk session for %s ended: %s", entry.id, exc)
            finally:
                try:
                    entry.camera.talk_stop()
                except HikError as exc:  # pragma: no cover - best effort
                    _LOGGER.debug("talk_stop failed: %s", exc)
                with entry.lock:
                    entry.talking = False
                ws.close()
                _LOGGER.info("talk session for %s closed after %d frame(s)",
                             entry.id, frames)

    return Handler


def serve(cameras: list[CameraEntry], host: str = "0.0.0.0", port: int = 8654,
          token: str | None = None, ssl_context=None,
          ssl_port: int | None = None) -> None:
    for entry in cameras:
        try:
            entry.camera.login()
            _LOGGER.info("camera %s ready (%s)", entry.id, entry.camera.serial)
        except HikError as exc:
            # One unreachable camera must not take the whole bridge down. The
            # request path retries the login before it uses the camera.
            _LOGGER.error("camera %s is not reachable yet: %s", entry.id, exc)

    https_port: int | None = None
    if ssl_context is not None:
        try:
            https_port = int(ssl_port or port + 1)
        except (TypeError, ValueError):
            https_port = port + 1

    state = _State(cameras, token, https_port)
    httpd = ThreadingHTTPServer((host, port), _handler(state))
    httpd.daemon_threads = True
    _LOGGER.info("hikbridge listening on http://%s:%s with %d camera(s)",
                 host, port, len(cameras))

    if ssl_context is not None:
        # Browsers only hand out microphone access on secure origins, so the
        # HTTPS listener is what makes press-to-talk work. It deliberately
        # runs on a second port: Supervisor's ingress and the HA/Scrypted
        # HTTP clients keep talking plain HTTP to the first one.
        httpsd = ThreadingHTTPServer((host, https_port), _handler(state))
        httpsd.daemon_threads = True
        httpsd.socket = ssl_context.wrap_socket(httpsd.socket, server_side=True)
        threading.Thread(target=httpsd.serve_forever, name="hikbridge-https",
                         daemon=True).start()
        _LOGGER.info(
            "hikbridge HTTPS listening on https://%s:%s (press-to-talk)",
            host, https_port,
        )
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
        for entry in cameras:
            try:
                entry.camera.close()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                pass
