#!/usr/bin/env bash
set -euo pipefail

OPTIONS=/data/options.json

read_opt() {
    python3 - "$1" <<'PY'
import json, sys
with open('/data/options.json') as fh:
    data = json.load(fh)
print(data.get(sys.argv[1], '') or '')
PY
}

CAMERA_IP=$(read_opt camera_ip)
CAMERA_PORT=$(read_opt camera_port)
USERNAME=$(read_opt username)
PASSWORD=$(read_opt password)
CHANNEL=$(read_opt channel)
SPEAK_RATE=$(read_opt speak_sample_rate)
TOKEN=$(read_opt auth_token)

export HIK_IP="$CAMERA_IP"
export HIK_PORT="${CAMERA_PORT:-8000}"
export HIK_USER="${USERNAME:-admin}"
export HIK_PASSWORD="$PASSWORD"
export HIK_CHANNEL="${CHANNEL:-1}"
export HIK_SPEAK_RATE="${SPEAK_RATE:-16000}"
export HIK_TOKEN="$TOKEN"
export HIK_LISTEN="0.0.0.0"
export HIK_LISTEN_PORT="8654"

echo "hikbridge: camera ${HIK_IP}:${HIK_PORT} channel ${HIK_CHANNEL}"
exec python3 -m hikbridge --verbose
