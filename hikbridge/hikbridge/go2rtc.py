"""Small manager for the optional bundled go2rtc WebRTC gateway.

go2rtc is deliberately kept on a loopback API port.  The Hikbridge HTTP
server proxies only the SDP exchange, so the browser never gets an
unauthenticated management API.  WebRTC media itself uses the normal
go2rtc UDP/TCP listener on the LAN.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

_LOGGER = logging.getLogger("hikbridge.go2rtc")


class Go2RTCError(RuntimeError):
    """Raised when the WebRTC gateway cannot start or exchange SDP."""


def _stream_name(entry) -> str:
    value = re.sub(r"[^A-Za-z0-9_-]+", "-", str(entry.id)).strip("-")
    return "hikbridge-" + (value or "camera")


class Go2RTCManager:
    """Start one go2rtc process and expose its WHEP-style SDP exchange."""

    def __init__(self, cameras, binary: str | None = None,
                 api_port: int = 1984, media_port: int = 8555,
                 candidates: list[str] | None = None) -> None:
        self.cameras = [entry for entry in cameras if entry.rtsp_url]
        self.binary = (
            binary
            or os.environ.get("GO2RTC_BIN")
            or shutil.which("go2rtc")
            or "/usr/local/bin/go2rtc"
        )
        self.api_port = int(api_port)
        self.media_port = int(media_port)
        self.candidates = [
            str(value).strip() for value in (candidates or []) if str(value).strip()
        ]
        self.available = False
        self.error: str | None = None
        self._proc: subprocess.Popen | None = None
        self._log_thread: threading.Thread | None = None
        self._config_dir: str | None = None
        self._config_path: str | None = None
        self._names = {_stream_name(entry): entry for entry in self.cameras}

    @property
    def configured(self) -> bool:
        return bool(self.cameras)

    @property
    def stream_names(self) -> dict[str, object]:
        return dict(self._names)

    def status(self) -> dict:
        return {
            "enabled": self.configured,
            "available": self.available,
            "error": self.error,
            "port": self.media_port if self.available else None,
        }

    def start(self) -> bool:
        """Start go2rtc, returning False when the optional path is unusable."""
        if not self.configured:
            self.error = "no camera has rtsp_url"
            return False
        if self.available:
            return True
        if not self.binary or not os.path.isfile(self.binary):
            self.error = f"go2rtc binary not found: {self.binary}"
            _LOGGER.warning("WebRTC disabled: %s", self.error)
            return False
        if not os.access(self.binary, os.X_OK):
            self.error = f"go2rtc is not executable: {self.binary}"
            _LOGGER.warning("WebRTC disabled: %s", self.error)
            return False

        try:
            self._write_config()
            self._proc = subprocess.Popen(
                [self.binary, "-config", self._config_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                text=True,
            )
        except OSError as exc:
            self.error = f"cannot start go2rtc: {exc}"
            _LOGGER.warning("WebRTC disabled: %s", self.error)
            self._cleanup_config()
            return False

        self._log_thread = threading.Thread(
            target=self._drain_logs,
            name="hikbridge-go2rtc-log",
            daemon=True,
        )
        self._log_thread.start()

        # The API is the readiness probe.  It also confirms that the process
        # did not immediately exit because the media port is already in use.
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                self.error = (
                    f"go2rtc exited with code {self._proc.returncode}; "
                    "check whether the WebRTC port is already in use"
                )
                break
            try:
                self._request("GET", "/api")
                self.available = True
                self.error = None
                _LOGGER.info(
                    "WebRTC gateway ready on UDP/TCP %s (API 127.0.0.1:%s)",
                    self.media_port, self.api_port,
                )
                return True
            except Go2RTCError:
                time.sleep(0.15)

        if not self.error:
            self.error = "go2rtc did not become ready in time"
        _LOGGER.warning("WebRTC disabled: %s", self.error)
        self.stop()
        return False

    def stop(self) -> None:
        self.available = False
        proc = self._proc
        self._proc = None
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    proc.kill()
                except OSError:
                    pass
        self._cleanup_config()

    def exchange(self, entry, offer: str) -> str:
        """Forward one browser SDP offer and return go2rtc's SDP answer."""
        if not self.available:
            raise Go2RTCError(self.error or "WebRTC gateway is not available")
        name = _stream_name(entry)
        if name not in self._names:
            raise Go2RTCError(f"camera {entry.id} has no WebRTC stream")
        body = json.dumps({"type": "offer", "sdp": offer}).encode("utf-8")
        response = self._request(
            "POST",
            "/api/webrtc?src=" + urllib.parse.quote(name, safe=""),
            body,
            {"Content-Type": "application/json"},
            timeout=10.0,
        )
        try:
            payload = json.loads(response.decode("utf-8"))
            answer = str(payload.get("sdp") or "")
        except (ValueError, UnicodeDecodeError) as exc:
            raise Go2RTCError(f"invalid go2rtc response: {exc}") from exc
        if not answer:
            raise Go2RTCError("go2rtc returned an empty SDP answer")
        return answer

    def _write_config(self) -> None:
        self._config_dir = tempfile.mkdtemp(prefix="hikbridge-go2rtc-")
        self._config_path = os.path.join(self._config_dir, "go2rtc.yaml")
        lines = [
            "api:",
            f'  listen: "127.0.0.1:{self.api_port}"',
            "webrtc:",
            f'  listen: ":{self.media_port}"',
            "  ice_servers: []",
        ]
        if self.candidates:
            lines.append("  candidates:")
            for candidate in self.candidates:
                lines.append(f"    - {json.dumps(candidate)}")
        lines += [
            "streams:",
        ]
        for name, entry in self._names.items():
            url = str(entry.rtsp_url)
            # Keep the transport on TCP for the same reason the MSE path does:
            # packet loss on Wi-Fi otherwise shows up as frozen video.
            if "#" not in url:
                url += "#transport=tcp"
            lines.append(f"  {name}: {json.dumps(url, ensure_ascii=True)}")
        with open(self._config_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

    def _cleanup_config(self) -> None:
        path = self._config_dir
        self._config_dir = None
        self._config_path = None
        if path:
            shutil.rmtree(path, ignore_errors=True)

    def _drain_logs(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for line in iter(proc.stdout.readline, ""):
            text = line.strip()
            if text:
                _LOGGER.debug("go2rtc: %s", text)

    def _request(self, method: str, path: str, body: bytes | None = None,
                 headers: dict[str, str] | None = None,
                 timeout: float = 2.0) -> bytes:
        url = f"http://127.0.0.1:{self.api_port}{path}"
        request = urllib.request.Request(
            url,
            data=body,
            headers=headers or {},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace").strip()
            raise Go2RTCError(
                f"go2rtc HTTP {exc.code}: {detail or exc.reason}"
            ) from exc
        except (OSError, urllib.error.URLError) as exc:
            raise Go2RTCError(f"go2rtc API unavailable: {exc}") from exc
