#!/usr/bin/env sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$ROOT"

port="$(sed -n 's/^ALPHADESK_PORT=//p' .env | head -n 1)"
port="${port:-8080}"
url="http://127.0.0.1:${port}"
curl --fail --silent --show-error "${url}/health/ready" >/dev/null
echo "AlphaDesk is ready: ${url}"
