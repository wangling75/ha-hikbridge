"""Small manager for the optional bundled go2rtc WebRTC gateway.

go2rtc is deliberately kept on a loopback API port.  The Hikbridge HTTP
server proxies only the SDP exchange, so the browser never gets an
unauthenticated management API.  WebRTC media itself uses the normal
go2rtc UDP/TCP listener on the LAN.

Two backends are supported:

* ``bundled`` - the add-on starts the go2rtc binary from its own image and
  binds a dedicated media port.  This is the only option outside Home
  Assistant (plain Docker), where the operator forwards that port.
* ``home_assistant`` - Home Assistant already runs its own go2rtc (API on
  127.0.0.1:11984, WebRTC on :18555/tcp).  The add-on registers its
  cameras there and reuses the SDP exchange, so no extra plugin port is
  ever exposed.  This mirrors how the HA WebRTC camera card works: media
  uses HA's own WebRTC port when it is reachable and the MSE fallback
  keeps working through the HA HTTP port (ingress) when it is not.
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

# Home Assistant's built-in go2rtc instance.  HA writes a temporary config
# with these listeners, so the add-on can reuse them without binding
# anything of its own:
#   api:   127.0.0.1:11984  (loopback only)
#   rtsp:  127.0.0.1:18554
#   webrtc: :18555/tcp
HA_GO2RTC_API = "http://127.0.0.1:11984"
HA_GO2RTC_WEBRTC_PORT = 18555


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
                 ice_servers: list[str] | None = None,
                 external_api: str | None = None,
                 advertise_port: int | None = None) -> None:
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
        # When set, the add-on does not start its own go2rtc: it registers
        # the cameras with an already running instance (Home Assistant's)
        # and only proxies the SDP exchange.
        self.external_api = str(external_api).rstrip("/") if external_api else None
        self.advertise_port = int(advertise_port or media_port)
        self.backend = "home_assistant" if self.external_api else "bundled"
        self._registered = False
        # Cameras with an audio codec get a second, Opus-only source so
        # browsers can hear them inside WebRTC.  If that source turns out
        # to be unusable the exchange retries video-only.
        self._audio_disabled: set[str] = set()
        selected_ice_servers = ice_servers or []
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

    def stream_name(self, entry) -> str:
        """Return the go2rtc stream name used for one camera."""
        return _stream_name(entry)

    def proxy(self, path: str, timeout: float = 15.0) -> tuple[int, str, bytes]:
        """Fetch a go2rtc HTTP endpoint so the add-on can re-serve it.

        This is what makes HLS work the way Home Assistant's own camera
        cards do remotely: the browser keeps talking to the HA HTTP port,
        and every playlist or segment request is a short round trip that
        survives proxies which buffer long-lived streaming responses.
        """
        base = self.external_api or f"http://127.0.0.1:{self.api_port}"
        request = urllib.request.Request(f"{base}{path}")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return (
                    int(response.status),
                    str(response.headers.get("Content-Type") or ""),
                    response.read(),
                )
        except urllib.error.HTTPError as exc:
            return (
                int(exc.code),
                str(exc.headers.get("Content-Type") or ""),
                exc.read(),
            )
        except (OSError, urllib.error.URLError) as exc:
            raise Go2RTCError(f"go2rtc API unavailable: {exc}") from exc

    def status(self) -> dict:
        return {
            "enabled": self.configured,
            "available": self.available,
            "error": self.error,
            "backend": self.backend,
            "port": self.advertise_port if self.available else None,
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
        if self.external_api:
            return self._start_external()
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

    def _start_external(self) -> bool:
        """Reuse the go2rtc that Home Assistant already runs.

        Nothing is started here.  The cameras are published as
        ``hikbridge-<id>`` streams through the loopback REST API and the SDP
        exchange is proxied exactly like the bundled backend.  Streams live
        in go2rtc memory only, so they are re-registered on demand after a
        Home Assistant restart.
        """
        try:
            info = json.loads(self._request("GET", "/api", timeout=3.0))
        except (Go2RTCError, ValueError) as exc:
            self.error = f"Home Assistant go2rtc API unavailable at {self.external_api}: {exc}"
            _LOGGER.info("WebRTC gateway not reused: %s", self.error)
            return False
        if not isinstance(info, dict) or not info.get("version"):
            self.error = (
                f"{self.external_api} does not look like a go2rtc API"
            )
            _LOGGER.info("WebRTC gateway not reused: %s", self.error)
            return False

        try:
            missing = self._register_external_streams()
        except Go2RTCError as exc:
            self.error = f"cannot publish cameras to the HA go2rtc: {exc}"
            _LOGGER.warning("WebRTC disabled: %s", self.error)
            return False
        if missing:
            self.error = (
                "the HA go2rtc did not accept the camera streams: "
                + ", ".join(sorted(missing))
            )
            _LOGGER.warning("WebRTC disabled: %s", self.error)
            return False

        self.available = True
        self.error = None
        self._registered = True
        _LOGGER.info(
            "Reusing the Home Assistant go2rtc at %s (version %s, WebRTC TCP %s)",
            self.external_api, info.get("version"), self.advertise_port,
        )
        _LOGGER.info(
            "WebRTC streams registered: %s", ", ".join(sorted(self._names))
        )
        return True

    def _register_external_streams(self, force: bool = False) -> set[str]:
        """Publish every camera in the external go2rtc, then verify.

        Returns the names go2rtc still does not know about.  A PUT can answer
        with an error while the stream was in fact created (go2rtc reports a
        failed config write that way), so the stream list is the source of
        truth.

        ``force`` re-publishes the sources even when the name already exists,
        which go2rtc replaces wholesale.
        """
        streams = self._external_streams()
        for name in self._names:
            if name in streams and not force:
                continue
            query = [("name", name)]
            query += [
                ("src", source)
                for source in self._sources(name, with_audio=name not in self._audio_disabled)
            ]
            path = "/api/streams?" + urllib.parse.urlencode(query)
            try:
                self._request("PUT", path, timeout=6.0)
            except Go2RTCError as exc:
                _LOGGER.debug("go2rtc PUT %s answered: %s", name, exc)
        streams = self._external_streams()
        return {name for name in self._names if name not in streams}

    def _external_streams(self) -> set[str]:
        payload = json.loads(self._request("GET", "/api/streams", timeout=5.0))
        if not isinstance(payload, dict):
            raise Go2RTCError("go2rtc returned an unexpected stream list")
        return {str(name) for name in payload}

    def _sources(self, name: str, with_audio: bool = True) -> list[str]:
        """Return the go2rtc sources for one camera.

        The transport stays on TCP for the same reason the MSE path uses it:
        packet loss on Wi-Fi otherwise shows up as frozen video.  A second
        source transcodes only the audio to Opus because browsers cannot
        decode the camera's AAC inside WebRTC.

        go2rtc reads ``#transport=tcp`` correctly from a config file, but a
        source registered through the REST API mangles the fragment, so
        API-registered streams stay on the plain URL.  go2rtc's RTSP client
        already uses TCP interleaving by default, so nothing is lost.
        """
        entry = self._names[name]
        url = str(entry.rtsp_url)
        if "#" not in url and not self.external_api:
            url += "#transport=tcp"
        sources = [url]
        # The SDK's audio_codec describes the private-protocol intercom
        # codec, not the RTSP live stream. Some cameras expose live audio
        # over RTSP even when NET_DVR_GetCurrentAudioCompress fails, so do
        # not use the intercom codec as a gate for live WebRTC audio.
        # go2rtc negotiates across sources and will ignore this one when the
        # RTSP stream has no audio track.
        if with_audio and name not in self._audio_disabled:
            # Keep failures per camera: a video-only model must not disable
            # Opus playback for the other cameras on the same gateway.
            sources.append(f"ffmpeg:{name}#audio=opus")
        return sources

    def stop(self) -> None:
        self.available = False
        if self.external_api and self._registered:
            self._registered = False
            for name in self._names:
                try:
                    self._request(
                        "DELETE",
                        "/api/streams?" + urllib.parse.urlencode({"src": name}),
                        timeout=3.0,
                    )
                except Go2RTCError as exc:
                    _LOGGER.debug("cannot remove go2rtc stream %s: %s", name, exc)
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
            if not (self.external_api and self._start_external()):
                raise Go2RTCError(self.error or "WebRTC gateway is not available")
        name = _stream_name(entry)
        if name not in self._names:
            raise Go2RTCError(f"camera {entry.id} has no WebRTC stream")
        if self.external_api:
            # Home Assistant's go2rtc keeps runtime streams in memory only;
            # a restart or a watchdog respawn drops them.
            try:
                if self._register_external_streams():
                    raise Go2RTCError(
                        "the HA go2rtc no longer has the Hikbridge camera streams"
                    )
            except Go2RTCError:
                self.available = False
                raise
        try:
            return self._post_offer(name, offer, request_host)
        except Go2RTCError as exc:
            # The optional Opus source needs go2rtc's own RTSP relay.  When
            # that relay is unavailable the whole offer fails, so retry once
            # with the camera's plain RTSP source and stay video-only.
            if name in self._audio_disabled:
                raise
            _LOGGER.warning(
                "WebRTC exchange for %s failed (%s); retrying without the "
                "Opus audio source", entry.id, exc,
            )
            self._audio_disabled.add(name)
            if self.external_api:
                if self._register_external_streams(force=True):
                    raise
            else:
                # The bundled gateway keeps its stream list in the YAML it
                # was started with, so restart it once with this camera's
                # audio source removed.
                self.stop()
                if not self.start():
                    raise Go2RTCError(self.error or "cannot restart WebRTC gateway")
            return self._post_offer(name, offer, request_host)

    def _post_offer(self, name: str, offer: str,
                    request_host: str | None) -> str:
        """Send one SDP offer to go2rtc and return its answer."""
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
        # The bundled gateway listens on UDP and TCP; Home Assistant's own
        # go2rtc is configured with TCP only, so advertising UDP there would
        # just waste ICE time.
        transports = [
            ("udp", "typ host"),
            ("tcp", "typ host tcptype passive"),
        ] if not self.external_api else [
            ("tcp", "typ host tcptype passive"),
        ]
        for index, address in enumerate(addresses, start=1):
            for offset, (transport, kind) in enumerate(transports, start=1):
                candidate_lines.append(
                    f"a=candidate:{900000 + index * 10 + offset} 1 "
                    f"{transport} 2105458943 {address} "
                    f"{self.advertise_port} {kind}"
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
        for name in self._names:
            lines.append(f"  {name}:")
            for source in self._sources(name):
                lines.append(f"    - {json.dumps(source, ensure_ascii=True)}")
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
        base = self.external_api or f"http://127.0.0.1:{self.api_port}"
        url = f"{base}{path}"
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
