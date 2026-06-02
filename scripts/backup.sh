#!/usr/bin/env sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p backups

timestamp="$(date +%Y%m%d-%H%M%S)"
archive="backups/alphadesk-backup-${timestamp}.tar.gz"
restart_services=true
if [ "${1:-}" = "--keep-stopped" ]; then
  restart_services=false
fi

restart_if_needed() {
  if [ "$restart_services" = "true" ]; then
    docker compose --env-file .env start
  fi
}

trap restart_if_needed EXIT INT TERM
docker compose --env-file .env stop
tar --exclude='data/logs' -czf "$archive" .env data
echo "Backup created: ${archive}"

if [ "$restart_services" = "true" ]; then
  docker compose --env-file .env start
fi
restart_services=false
trap - EXIT INT TERM
