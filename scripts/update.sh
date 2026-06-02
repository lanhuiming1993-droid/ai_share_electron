#!/usr/bin/env sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$ROOT"
sh ./scripts/backup.sh --keep-stopped
docker compose --env-file .env pull
docker compose --env-file .env up -d
echo "AlphaDesk images updated. Run ./scripts/health.sh to verify readiness."
