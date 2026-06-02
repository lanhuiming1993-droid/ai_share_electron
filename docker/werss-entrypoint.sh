#!/usr/bin/env bash
set -euo pipefail

plant="/app/env_$(uname -m)"
if [ -x "$plant/bin/python3" ]; then
  "$plant/bin/python3" /app/sync_werss_admin.py
fi

exec /app/start.sh
