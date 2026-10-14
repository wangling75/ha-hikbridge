#!/usr/bin/env bash
set -euo pipefail

# The add-on reads the camera list from /data/options.json. These variables
# only provide fallbacks for manual `docker run` use.
export HIK_OPTIONS_FILE="${HIK_OPTIONS_FILE:-/data/options.json}"
export HIK_SDK_DIR="${HIK_SDK_DIR:-/opt/hikbridge/hcnetsdk}"
export HIK_LISTEN="${HIK_LISTEN:-0.0.0.0}"
export HIK_LISTEN_PORT="${HIK_LISTEN_PORT:-8654}"
export TZ="${HIK_TIMEZONE:-Asia/Shanghai}"

echo "hikbridge: starting, options from ${HIK_OPTIONS_FILE}"
exec python3 -m hikbridge --verbose
