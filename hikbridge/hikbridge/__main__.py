"""Command line entry point for the hikbridge add-on.

Inside the add-on the configuration comes from /data/options.json, which may
describe one camera (legacy keys) or a list of cameras:

    {
      "cameras": [
        {"id": "cam207", "name": "客厅", "camera_ip": "192.168.2.207",
         "camera_port": 8000, "username": "admin", "password": "…",
         "channel": 1,
         "rtsp_url": "rtsp://…"},
        {"id": "cam225", "name": "院子", "camera_ip": "192.168.2.225", …}
      ],
      "speak_sample_rate": 16000,
      "auth_token": ""
    }

The old single-camera keys (camera_ip, username, …) and the command line flags
still work, so an existing install keeps running after the update.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys

from .server import CameraEntry, serve
from .sdk import HikCamera

_LOGGER = logging.getLogger("hikbridge")


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

    if not entries and _clean(options.get("camera_ip")):
        entry = _camera_entry(options, 0, sdk_dir, speak_rate)
        if entry is not None:
            entries.append(entry)

    if not entries:
        entry = _camera_entry(
            {
                "id": "cam1",
                "name": args.ip,
                "camera_ip": args.ip,
                "camera_port": args.port,
                "username": args.user,
                "password": args.password,
                "channel": args.channel,
            },
            0, sdk_dir, speak_rate,
        )
        if entry is not None:
            entries.append(entry)

    # Keep ids unique so the URL routing stays unambiguous.
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        base = entry.id
        suffix = 2
        while entry.id in seen:
            entry.id = f"{base}-{suffix}"
            suffix += 1
        seen.add(entry.id)

    return entries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hikvision/EZVIZ PTZ + intercom bridge")
    parser.add_argument("--options-file",
                        default=os.environ.get("HIK_OPTIONS_FILE", "/data/options.json"),
                        help="Home Assistant add-on options file")
    parser.add_argument("--ip", default=os.environ.get("HIK_IP", "192.168.2.207"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("HIK_PORT", 8000)))
    parser.add_argument("--user", default=os.environ.get("HIK_USER", "admin"))
    parser.add_argument("--password", default=os.environ.get("HIK_PASSWORD", ""))
    parser.add_argument("--channel", type=int, default=int(os.environ.get("HIK_CHANNEL", 1)))
    parser.add_argument("--sdk-dir", default=os.environ.get("HIK_SDK_DIR"))
    parser.add_argument("--speak-rate", type=int,
                        default=int(os.environ.get("HIK_SPEAK_RATE", 16000)))
    parser.add_argument("--listen", default=os.environ.get("HIK_LISTEN", "0.0.0.0"))
    parser.add_argument("--listen-port", type=int,
                        default=int(os.environ.get("HIK_LISTEN_PORT", 8654)))
    parser.add_argument("--token", default=os.environ.get("HIK_TOKEN"))
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

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

    for entry in cameras:
        _LOGGER.info("configured camera %s (%s) -> %s:%s channel %s",
                     entry.id, entry.name, entry.camera.ip, entry.camera.port,
                     entry.camera.channel)

    def _shutdown(signum, frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        serve(cameras, args.listen, args.listen_port, token)
    except SystemExit:
        for entry in cameras:
            try:
                entry.camera.close()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
