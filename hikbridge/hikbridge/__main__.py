"""Command line entry point for the hikbridge add-on.

Inside the add-on the configuration comes from /data/options.json, which may
describe a list of cameras:

    {
      "cameras": [
        {"id": "cam207", "name": "camera 207", "camera_ip": "192.168.2.207",
         "camera_port": 8000, "username": "admin", "password": "...",
         "channel": 1,
         "rtsp_url": "rtsp://..."},
        {"id": "cam225", "name": "camera 225", "camera_ip": "192.168.2.225"}
      ],
      "speak_sample_rate": 16000,
      "auth_token": ""
    }

The add-on no longer accepts top-level single-camera keys. A non-empty
"cameras" list is required.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
import urllib.request

import hikbridge  # absolute self-import: publishes the timezone status

from .server import CameraEntry, serve
from .go2rtc import (
    HA_GO2RTC_API,
    HA_GO2RTC_WEBRTC_PORT,
    Go2RTCManager,
)
from .sdk import HikCamera

_LOGGER = logging.getLogger("hikbridge")


def _apply_host_timezone() -> None:
    """Adopt the Home Assistant host timezone so log stamps match the UI.

    The container starts in UTC, which makes every log line look eight hours
    off on a UTC+8 host. Supervisor exposes the configured zone over its own
    API, and TZ has to be set before the first log record is formatted.
    """
    fallback_zone = os.environ.get("TZ") or "UTC"
    try:
        time.tzset()
    except AttributeError:  # pragma: no cover - Windows only
        pass

    hikbridge.timezone_status = f"fallback: {fallback_zone}"

    token = os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HASSIO_TOKEN")
    if not token:
        return
    try:
        request = urllib.request.Request(
            "http://supervisor/supervisor/info",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.load(response)
        # Supervisor wraps most responses in {"result": ..., "data": ...}.
        body = payload.get("data")
        if not isinstance(body, dict):
            body = payload
        zone = body.get("timezone")
    except Exception as exc:  # noqa: BLE001 - the timezone is cosmetic
        print(f"hikbridge: cannot read the host timezone: {exc}", file=sys.stderr)
        hikbridge.timezone_status = f"lookup failed: {exc}; using {fallback_zone}"
        return
    if not zone:
        hikbridge.timezone_status = (
            f"host did not report a timezone; using {fallback_zone}"
        )
        return
    os.environ["TZ"] = str(zone)
    try:
        time.tzset()
    except AttributeError:  # pragma: no cover - Windows only
        pass
    hikbridge.timezone_status = str(zone)
    print(f"hikbridge: timezone set to {zone}", file=sys.stderr)


def _read_options(path: str | None) -> dict:
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        print(f"hikbridge: cannot read {path}: {exc}", file=sys.stderr)
        return {}
    return data if isinstance(data, dict) else {}


def _clean(value, default=None):
    """Treat empty strings as 'not configured'."""
    if value is None:
        return default
    if isinstance(value, str) and not value.strip():
        return default
    return value


def _camera_entry(spec: dict, index: int, sdk_dir: str | None,
                  speak_rate: int) -> CameraEntry | None:
    ip = _clean(spec.get("camera_ip") or spec.get("ip"))
    if not ip:
        return None
    camera = HikCamera(
        ip=str(ip),
        port=int(_clean(spec.get("camera_port") or spec.get("port"), 8000)),
        username=str(_clean(spec.get("username"), "admin")),
        password=str(_clean(spec.get("password"), "")),
        channel=int(_clean(spec.get("channel"), 1)),
        sdk_dir=sdk_dir,
        speak_sample_rate=speak_rate,
    )
    return CameraEntry(
        id=str(_clean(spec.get("id"), "") or f"cam{index + 1}"),
        name=str(_clean(spec.get("name"), "") or str(ip)),
        camera=camera,
        rtsp_url=_clean(spec.get("rtsp_url")),
    )


def _build_cameras(options: dict, args: argparse.Namespace) -> list[CameraEntry]:
    sdk_dir = _clean(args.sdk_dir) or os.environ.get("HIK_SDK_DIR")
    try:
        speak_rate = int(_clean(options.get("speak_sample_rate"), args.speak_rate))
    except (TypeError, ValueError):
        speak_rate = args.speak_rate

    entries: list[CameraEntry] = []
    raw = options.get("cameras")
    if isinstance(raw, list):
        for index, spec in enumerate(raw):
            if isinstance(spec, dict):
                entry = _camera_entry(spec, index, sdk_dir, speak_rate)
                if entry is not None:
                    entries.append(entry)

    # Keep ids unique so the URL routing stays unambiguous.
    seen: set[str] = set()
    for entry in entries:
        base = entry.id
        suffix = 2
        while entry.id in seen:
            entry.id = f"{base}-{suffix}"
            suffix += 1
        seen.add(entry.id)

    return entries


def _probe_go2rtc(api_url: str, timeout: float = 1.5) -> bool:
    """Return True when api_url answers like a go2rtc instance.

    Home Assistant starts its own go2rtc inside the HA container, which
    shares the host network namespace with this add-on.  A quick probe is
    all that is needed to tell whether reusing it is possible.
    """
    try:
        request = urllib.request.Request(api_url.rstrip("/") + "/api")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except Exception:  # noqa: BLE001 - any failure means 'not available'
        return False
    return isinstance(payload, dict) and bool(payload.get("version"))


def _build_webrtc(cameras: list[CameraEntry], options: dict) -> Go2RTCManager | None:
    """Create the WebRTC gateway manager for the configured backend.

    ``bundled`` starts the go2rtc binary from the add-on image and binds its
    own media port, which is what a plain Docker deployment forwards.
    ``home_assistant`` publishes the cameras to the go2rtc that Home
    Assistant already runs, so the plugin never exposes a port of its own.
    ``auto`` (the default) picks Home Assistant when its go2rtc answers on
    the loopback API and falls back to the bundled gateway otherwise.
    """
    if not bool(_clean(options.get("webrtc"), True)):
        return None

    # Old installs may still carry public STUN servers. go2rtc works fine
    # without them on the LAN, and HA ingress remote playback uses HLS.
    if options.get("webrtc_ice_servers"):
        _LOGGER.info("webrtc_ice_servers is deprecated and ignored")

    try:
        # HA's own go2rtc owns 11984 on the loopback interface, so the
        # bundled gateway stays one port above it.
        api_port = int(_clean(options.get("webrtc_api_port"), 11985))
        media_port = int(_clean(options.get("webrtc_port"), 18554))
        ha_port = int(_clean(options.get("ha_webrtc_port"), HA_GO2RTC_WEBRTC_PORT))
    except (TypeError, ValueError) as exc:
        _LOGGER.error("invalid WebRTC port configuration: %s", exc)
        return None

    raw_candidates = options.get("webrtc_candidates")
    webrtc_candidates = (
        [str(value) for value in raw_candidates if str(value).strip()]
        if isinstance(raw_candidates, list) else []
    )

    backend = str(_clean(options.get("webrtc_backend"), "auto")).strip().lower()
    if backend not in ("auto", "bundled", "home_assistant"):
        _LOGGER.warning("unknown webrtc_backend %r, using auto", backend)
        backend = "auto"
    ha_api = str(_clean(options.get("ha_go2rtc_api"), HA_GO2RTC_API)).rstrip("/")

    if backend in ("auto", "home_assistant"):
        if backend == "auto" and not _probe_go2rtc(ha_api):
            _LOGGER.info(
                "no go2rtc at %s, using the bundled WebRTC gateway", ha_api
            )
        else:
            shared = Go2RTCManager(
                cameras,
                api_port=api_port,
                media_port=media_port,
                candidates=webrtc_candidates,
                external_api=ha_api,
                advertise_port=ha_port,
            )
            if shared.start():
                return shared
            _LOGGER.warning(
                "cannot reuse the Home Assistant go2rtc (%s); "
                "starting the bundled WebRTC gateway instead", shared.error,
            )

    return Go2RTCManager(
        cameras,
        api_port=api_port,
        media_port=media_port,
        candidates=webrtc_candidates,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hikvision/EZVIZ PTZ + intercom bridge")
    parser.add_argument("--options-file",
                        default=os.environ.get("HIK_OPTIONS_FILE", "/data/options.json"),
                        help="Home Assistant add-on options file")
    parser.add_argument("--sdk-dir", default=os.environ.get("HIK_SDK_DIR"))
    parser.add_argument("--speak-rate", type=int,
                        default=int(os.environ.get("HIK_SPEAK_RATE", 16000)))
    parser.add_argument("--listen", default=os.environ.get("HIK_LISTEN", "0.0.0.0"))
    parser.add_argument("--listen-port", type=int,
                        default=int(os.environ.get("HIK_LISTEN_PORT", 8654)))
    parser.add_argument("--token", default=os.environ.get("HIK_TOKEN"))
    parser.add_argument("--ssl", action="store_true",
                        default=os.environ.get("HIK_SSL", "").lower() in ("1", "true", "yes"))
    parser.add_argument("--ssl-port", type=int,
                        default=int(os.environ.get("HIK_SSL_PORT", 8655)))
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    _apply_host_timezone()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    options = _read_options(args.options_file)
    cameras = _build_cameras(options, args)
    if not cameras:
        print("hikbridge: no camera is configured", file=sys.stderr)
        return 2

    token = _clean(options.get("auth_token")) or args.token

    # WebRTC is an optional, self-contained playback path.  A missing or
    # unusable gateway must never prevent the existing PTZ, snapshot and MSE
    # features from loading.
    webrtc = _build_webrtc(cameras, options)

    # Browsers only allow microphone capture on secure origins, so HTTPS is
    # what makes the web UI press-to-talk button work.
    ssl_context = None
    ssl_port = args.ssl_port
    if bool(_clean(options.get("ssl"), False)) or args.ssl:
        try:
            ssl_port = int(_clean(options.get("ssl_port"), args.ssl_port))
        except (TypeError, ValueError):
            ssl_port = args.ssl_port
        try:
            from .tls import build_context
            ssl_context = build_context(
                _clean(options.get("certfile")), _clean(options.get("keyfile")),
            )
        except Exception as exc:  # noqa: BLE001 - fall back to plain HTTP
            _LOGGER.error("cannot enable HTTPS, staying on HTTP: %s", exc)
            ssl_context = None

    for entry in cameras:
        _LOGGER.info("configured camera %s (%s) -> %s:%s channel %s",
                     entry.id, entry.name, entry.camera.ip, entry.camera.port,
                     entry.camera.channel)

    def _shutdown(signum, frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        serve(cameras, args.listen, args.listen_port, token, ssl_context,
              ssl_port, webrtc)
    except SystemExit:
        for entry in cameras:
            try:
                entry.camera.close()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
