#!/usr/bin/env sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$ROOT"

random_secret() {
  openssl rand -base64 32 | tr '+/' '-_' | tr -d '=\n'
}

if [ ! -f .env ]; then
  if [ -d data/werss ] && [ -n "$(find data/werss -mindepth 1 -print -quit)" ]; then
    echo "Existing data/werss content was found but .env is missing. Create .env with the credentials used by the existing WeRSS database, or move data/werss aside before initializing a fresh deployment." >&2
    exit 1
  fi
  password="$(random_secret)"
  secret_key="$(random_secret)"
  sed \
    -e "s/change-me-password-generated-by-init/${password}/" \
    -e "s/change-me-secret-key-generated-by-init/${secret_key}/" \
    .env.example > .env
  echo "Created .env with generated WeRSS credentials."
else
  echo "Using existing .env."
fi

mkdir -p data/alphadesk data/werss backups
docker compose --env-file .env config --quiet
echo "AlphaDesk deployment directory is initialized."
