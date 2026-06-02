#!/usr/bin/env sh
set -eu

if [ "$#" -ne 1 ]; then
  echo "Usage: sh ./scripts/restore.sh backups/alphadesk-backup-YYYYMMDD-HHMMSS.tar.gz" >&2
  exit 1
fi

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
ARCHIVE="$(CDPATH= cd -- "$(dirname -- "$1")" && pwd)/$(basename -- "$1")"
cd "$ROOT"

if [ ! -f "$ARCHIVE" ]; then
  echo "Backup archive not found: $ARCHIVE" >&2
  exit 1
fi

if [ -f .env ]; then
  docker compose --env-file .env down
fi

rm -rf .restore-tmp
mkdir .restore-tmp
tar -xzf "$ARCHIVE" -C .restore-tmp
rm -rf data
cp .restore-tmp/.env .env
cp -R .restore-tmp/data data
rm -rf .restore-tmp
echo "Backup restored. Run sh ./scripts/start.sh to launch AlphaDesk."
