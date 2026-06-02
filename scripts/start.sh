#!/usr/bin/env sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$ROOT"

sh ./scripts/init.sh
if [ -f docker/frontend.Dockerfile ] && [ -f docker/backend.Dockerfile ]; then
  docker compose --env-file .env up -d --build
else
  docker compose --env-file .env pull
  docker compose --env-file .env up -d --no-build
fi

port="$(sed -n 's/^ALPHADESK_PORT=//p' .env | head -n 1)"
port="${port:-8080}"
url="http://127.0.0.1:${port}"

attempt=0
until curl --fail --silent --show-error "${url}/health/ready" >/dev/null; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 60 ]; then
    echo "AlphaDesk did not become ready in time. Run: docker compose logs --tail=200" >&2
    exit 1
  fi
  sleep 2
done

echo "AlphaDesk is ready: ${url}"
