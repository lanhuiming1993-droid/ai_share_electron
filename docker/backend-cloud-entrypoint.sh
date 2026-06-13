#!/usr/bin/env sh
set -eu

exec python -m uvicorn backend.main:app --host 0.0.0.0 --port 8765
