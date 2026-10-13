"""Tiny stdlib HTTP API in front of HikCamera.

Endpoints (all accept an optional X-Auth-Token header when a token is set):

  GET  /health                 -> {"ok": true, "serial": "..."}
  GET  /snapshot.jpg           -> image/jpeg
  POST /ptz   {"direction": "right", "duration": 0.6}
  POST /ptz/stop
  POST /speak                  -> body is a WAV file, played on the speaker
  POST /speak_raw?rate=8000    -> body is forwarded to the speaker verbatim
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from . import __version__
from .audio import prepare_wav
from .sdk import PTZ_COMMANDS, HikCamera, HikError

_LOGGER = logging.getLogger("hikbridge")


class _State:
    def __init__(self, camera: HikCamera, token: str | None) -> None:
        self.camera = camera
        self.token = token
        self.lock = threading.Lock()


def _handler(state: _State):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"hikbridge/{__version__}"

        def log_message(self, fmt, *args):  # noqa: A003
            _LOGGER.debug("%s - %s", self.address_string(), fmt % args)

        # -- helpers -------------------------------------------------------
        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, payload: dict) -> None:
            self._send(code, json.dumps(payload).encode(), "application/json")

        def _authorised(self) -> bool:
            if not state.token:
                return True
            if self.headers.get("X-Auth-Token") == state.token:
                return True
            self._json(401, {"ok": False, "error": "unauthorised"})
            return False

        def _body(self) -> bytes:
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(length) if length else b""

        # -- routes --------------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802
            if not self._authorised():
                return
            path = self.path.split("?", 1)[0]
            try:
                if path in ("/", "/health"):
                    self._json(200, {
                        "ok": True,
                        "version": __version__,
                        "serial": state.camera.serial,
                        "device": state.camera.device,
                    })
                elif path in ("/snapshot", "/snapshot.jpg"):
                    with state.lock:
                        with tempfile.TemporaryDirectory() as tmp:
                            target = os.path.join(tmp, "snap.jpg")
                            state.camera.snapshot(target)
                            with open(target, "rb") as fh:
                                data = fh.read()
                    self._send(200, data, "image/jpeg")
                else:
                    self._json(404, {"ok": False, "error": "not found"})
            except HikError as exc:
                self._json(502, {"ok": False, "error": str(exc)})

        def do_POST(self) -> None:  # noqa: N802
            if not self._authorised():
                return
            path = self.path.split("?", 1)[0]
            body = self._body()
            try:
                if path == "/ptz":
                    payload = json.loads(body or b"{}")
                    direction = payload.get("direction", "")
                    duration = float(payload.get("duration", 0.6))
                    if direction not in PTZ_COMMANDS:
                        self._json(400, {"ok": False, "error": "bad direction"})
                        return
                    with state.lock:
                        state.camera.nudge(direction, duration)
                    self._json(200, {"ok": True, "direction": direction, "duration": duration})
                elif path == "/ptz/stop":
                    with state.lock:
                        ok = state.camera.stop()
                    self._json(200, {"ok": bool(ok)})
                elif path == "/speak":
                    if not body:
                        self._json(400, {"ok": False, "error": "empty body"})
                        return
                    pcm = prepare_wav(body, state.camera.speak_sample_rate)
                    with state.lock:
                        sent = state.camera.speak(pcm)
                    self._json(200, {"ok": True, "bytes": sent})
                elif path == "/speak_raw":
                    # escape hatch for experiments: bytes are forwarded verbatim
                    if not body:
                        self._json(400, {"ok": False, "error": "empty body"})
                        return
                    query = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
                    rate = int(query.get("rate", [state.camera.speak_sample_rate])[0])
                    with state.lock:
                        sent = state.camera.speak(body, rate)
                    self._json(200, {"ok": True, "bytes": sent})
                else:
                    self._json(404, {"ok": False, "error": "not found"})
            except HikError as exc:
                self._json(502, {"ok": False, "error": str(exc)})
            except (ValueError, OSError) as exc:
                self._json(400, {"ok": False, "error": str(exc)})

    return Handler


def serve(camera: HikCamera, host: str = "0.0.0.0", port: int = 8654,
          token: str | None = None) -> None:
    camera.login()
    state = _State(camera, token)
    httpd = ThreadingHTTPServer((host, port), _handler(state))
    _LOGGER.info("hikbridge listening on %s:%s (device %s)", host, port, camera.serial)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
        camera.close()
