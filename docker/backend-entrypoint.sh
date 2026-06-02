#!/usr/bin/env sh
set -eu

export DISPLAY="${DISPLAY:-:99}"

Xvfb "$DISPLAY" -screen 0 1440x960x24 -ac -nolisten tcp >/tmp/alphadesk-xvfb.log 2>&1 &
sleep 1
openbox-session >/tmp/alphadesk-openbox.log 2>&1 &
x11vnc -display "$DISPLAY" -forever -shared -nopw -localhost -rfbport 5900 >/tmp/alphadesk-x11vnc.log 2>&1 &
websockify --web=/usr/share/novnc/ 0.0.0.0:7900 localhost:5900 >/tmp/alphadesk-websockify.log 2>&1 &

exec python -m uvicorn backend.main:app --host 0.0.0.0 --port 8765
