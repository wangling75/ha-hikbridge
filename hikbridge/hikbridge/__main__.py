"""Command line entry point: python -m hikbridge --ip 192.168.2.207 ..."""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading

from .server import serve
from .sdk import HikCamera


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hikvision/EZVIZ PTZ bridge")
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

    camera = HikCamera(
        ip=args.ip, port=args.port, username=args.user, password=args.password,
        channel=args.channel, sdk_dir=args.sdk_dir, speak_sample_rate=args.speak_rate,
    )

    stopped = threading.Event()

    def _shutdown(signum, frame):
        stopped.set()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        serve(camera, args.listen, args.listen_port, args.token)
    except SystemExit:
        camera.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
