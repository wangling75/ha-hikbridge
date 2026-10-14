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
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

_LOGGER = logging.getLogger("hikbridge.go2rtc")

_DEFAULT_ICE_SERVERS = [
    "stun:stun.cloudflare.com:3478",
    "stun:stun.l.google.com:19302",
]

def _parse_ice_server(value: str) -> dict | None:
    """Convert a compact option into go2rtc's ICE server shape."""
    text = str(value or "").strip()
    if not text:
        return None
    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        urls = payload.get("urls", payload.get("url"))
        if isinstance(urls, str):
            urls = [urls]
        if not isinstance(urls, list):
            return None
        result: dict[str, object] = {
            "urls": [str(url) for url in urls if str(url)],
        }
        if not result["urls"]:
            return None
        if payload.get("username") is not None:
            result["username"] = str(payload["username"])
        if payload.get("credential") is not None:
            result["credential"] = str(payload["credential"])
        return result

    lower = text.lower()
    if not lower.startswith(("stun:", "turn:", "turns:")):
        return None
    if lower.startswith("stun:"):
        return {"urls": [text]}

    scheme, rest = text.split(":", 1)
    if "@" not in rest:
        return {"urls": [text]}
    userinfo, endpoint = rest.rsplit("@", 1)
    username, separator, credential = userinfo.partition(":")
    if not separator:
        return {"urls": [text]}
    return {
        "urls": [f"{scheme}:{endpoint}"],
        "username": urllib.parse.unquote(username),
        "credential": urllib.parse.unquote(credential),
    }


class Go2RTCError(RuntimeError):
    """Raised when the WebRTC gateway cannot start or exchange SDP."""


def _stream_name(entry) -> str:
    value = re.sub(r"[^A-Za-z0-9_-]+", "-", str(entry.id)).strip("-")
    return "hikbridge-" + (value or "camera")


class Go2RTCManager:
    """Start one go2rtc process and expose its WHEP-style SDP exchange."""

    def __init__(self, cameras, binary: str | None = None,
                 api_port: int = 11984, media_port: int = 18554,
                 candidates: list[str] | None = None,
                 ice_servers: list[str] | None = None) -> None:
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
        selected_ice_servers = (
            _DEFAULT_ICE_SERVERS if ice_servers is None else ice_servers
        )
        self.ice_servers = [
            str(value).strip() for value in selected_ice_servers if str(value).strip()
        ]
        self.ice_ips: list[str] = []
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
            "candidates": list(self.candidates),
            "ice_ips": list(self.ice_ips),
            "ice_servers": list(self.ice_servers),
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
            if not self.candidates:
                self.ice_ips = self._detect_ice_ips()
                self.candidates = [
                    f"{address}:{self.media_port}" for address in self.ice_ips
                ]
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
                if self._proc.poll() is not None:
                    self.error = (
                        f"go2rtc exited with code {self._proc.returncode}; "
                        "check whether the WebRTC port is already in use"
                    )
                    break
                streams = json.loads(self._request("GET", "/api/streams"))
                if not isinstance(streams, dict) or not set(self._names).issubset(streams):
                    self.error = (
                        "go2rtc API belongs to another instance or did not load "
                        "the Hikbridge camera streams"
                    )
                    break
                self.available = True
                self.error = None
                _LOGGER.info(
                    "WebRTC gateway ready on UDP/TCP %s (API 127.0.0.1:%s)",
                    self.media_port, self.api_port,
                )
                _LOGGER.info(
                    "WebRTC candidates=%s ice_ips=%s",
                    self.candidates, self.ice_ips,
                )
                return True
            except (Go2RTCError, ValueError) as exc:
                self.error = f"go2rtc readiness failed: {exc}"
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

    def exchange(self, entry, offer: str, request_host: str | None = None) -> str:
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
        if request_host:
            answer = self._add_request_candidate(answer, request_host)
        return answer

    def _add_request_candidate(self, sdp: str, request_host: str) -> str:
        """Add the browser-visible host as an ICE candidate when needed.

        The configured candidates describe the HA host's LAN address. When
        the UI is opened through a domain or public reverse proxy, that LAN
        address is not reachable. The request Origin/Referer gives us the
        hostname the browser actually used, so we can append a matching
        candidate without hard-coding a deployment-specific address.
        """
        host = str(request_host or "").strip().lower()
        if host.startswith("[") and "]" in host:
            host = host[1:host.index("]")]
        elif host.count(":") == 1:
            host = host.rsplit(":", 1)[0]
        if not host or host in {"localhost", "127.0.0.1", "::1"}:
            return sdp
        if not re.fullmatch(r"[a-z0-9.-]+", host):
            return sdp

        addresses: list[str] = []
        try:
            for item in socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_DGRAM):
                address = str(item[4][0])
                if address not in addresses:
                    addresses.append(address)
        except OSError:
            return sdp

        advertised = {candidate.rsplit(":", 1)[0] for candidate in self.candidates}
        addresses = [address for address in addresses if address not in advertised]
        addresses = addresses[:1]
        if not addresses:
            return sdp

        candidate_lines = []
        for index, address in enumerate(addresses, start=1):
            candidate_lines.append(
                f"a=candidate:900000{index:04d} 1 udp 2105458943 "
                f"{address} {self.media_port} typ host"
            )
            candidate_lines.append(
                f"a=candidate:900001{index:04d} 1 tcp 2105458943 "
                f"{address} {self.media_port} typ host tcptype passive"
            )

        lines: list[str] = []
        added = False
        for line in sdp.splitlines():
            lines.append(line)
            if not added and line.startswith("a=mid:"):
                lines.extend(candidate_lines)
                added = True
        if not added:
            lines.extend(candidate_lines)
        return "\r\n".join(lines) + "\r\n"

    def _write_config(self) -> None:
        self._config_dir = tempfile.mkdtemp(prefix="hikbridge-go2rtc-")
        self._config_path = os.path.join(self._config_dir, "go2rtc.yaml")
        lines = [
            "api:",
            f'  listen: "127.0.0.1:{self.api_port}"',
            # go2rtc otherwise starts its RTSP relay on :8554, which is the
            # default HA go2rtc port. Hikbridge never consumes this relay;
            # port 0 asks the OS for an unused loopback port instead.
            "rtsp:",
            '  listen: "127.0.0.1:0"',
            "webrtc:",
            f'  listen: ":{self.media_port}"',
        ]
        if self.ice_servers:
            lines.append("  ice_servers:")
            for raw_server in self.ice_servers:
                server = _parse_ice_server(raw_server)
                if server is None:
                    _LOGGER.warning("ignoring invalid WebRTC ICE server: %s", raw_server)
                    continue
                urls = server.get("urls") or []
                lines.append("    - urls:")
                for url in urls:
                    lines.append(f"        - {json.dumps(str(url))}")
                if server.get("username") is not None:
                    lines.append(
                        f"      username: {json.dumps(str(server['username']))}"
                    )
                if server.get("credential") is not None:
                    lines.append(
                        f"      credential: {json.dumps(str(server['credential']))}"
                    )
        else:
            lines.append("  ice_servers: []")
        if self.candidates:
            lines.append("  candidates:")
            for candidate in self.candidates:
                lines.append(f"    - {json.dumps(candidate)}")
        if self.ice_ips:
            lines += [
                "  filters:",
                "    ips:",
            ]
            for address in self.ice_ips:
                lines.append(f"      - {json.dumps(address)}")
        lines += [
            "streams:",
        ]
        for name, entry in self._names.items():
            url = str(entry.rtsp_url)
            # Keep the transport on TCP for the same reason the MSE path does:
            # packet loss on Wi-Fi otherwise shows up as frozen video.
            if "#" not in url:
                url += "#transport=tcp"
            lines.append(f"  {name}:")
            lines.append(f"    - {json.dumps(url, ensure_ascii=True)}")
            # Browsers do not decode AAC in WebRTC. go2rtc can reuse the
            # RTSP source and transcode only its audio to Opus; cameras
            # without an intercom/audio codec stay on the video-only source.
            if getattr(entry.camera, "audio_codec", None):
                lines.append(
                    f"    - {json.dumps(f'ffmpeg:{name}#audio=opus', ensure_ascii=True)}"
                )
        with open(self._config_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

    def _detect_ice_ips(self) -> list[str]:
        """Find the host-side address used to reach each camera.

        HA add-ons with host networking can still expose the Supervisor
        bridge address first. go2rtc then advertises that unreachable
        address in ICE. A UDP route lookup to each camera gives the actual
        LAN source address without relying on a user-specific hard-coded IP.
        """
        addresses: list[str] = []
        for entry in self.cameras:
            camera = entry.camera
            host = str(getattr(camera, "ip", "") or "")
            port = int(getattr(camera, "port", 8000) or 8000)
            if not host:
                continue
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                    sock.settimeout(0.5)
                    sock.connect((host, port))
                    address = str(sock.getsockname()[0])
            except OSError as exc:
                _LOGGER.debug(
                    "cannot detect WebRTC source address for %s: %s", host, exc
                )
                continue
            if address and address not in addresses:
                addresses.append(address)
        if not addresses:
            _LOGGER.warning(
                "WebRTC could not auto-detect a LAN address; "
                "set webrtc_candidates if the browser cannot connect"
            )
        return addresses

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
