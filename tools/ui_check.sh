#!/usr/bin/env bash
# Browser checks for the web UI. Needs no GPU and never touches live data.
#
#   ./tools/ui_check.sh            # screenshots land in /tmp/photodump-ui-check/
#
# Builds the current tree into its own compose project on :8099 with a
# scratch data dir seeded by tools/ui_fixture.py, points COMFY_HOST at an
# unroutable address so node jobs stay queued, starts headless Chromium,
# and runs tools/ui_check.py. Exits non-zero on any failed check or any
# uncaught exception in the page.
set -uo pipefail
cd "$(dirname "$0")/.."

PROJ=photodump-uicheck
export CONTAINER_NAME=photodump-uicheck
PORT=8099
DEBUG_PORT=9231
DATA_SUB=_uicheck
SHOTS=${SHOTS:-/tmp/photodump-ui-check}
CHROME=${CHROME:-$HOME/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome}
IMAGE=${PROJ}-photodump
PROFILE=$(mktemp -d)

cleanup() {
  [ -n "${CHROME_PID:-}" ] && kill "$CHROME_PID" 2>/dev/null
  PORT=$PORT DATA_DIR=/srv/data/$DATA_SUB docker compose -p $PROJ down >/dev/null 2>&1
  # Files are container-owned (root); remove them from inside a container.
  docker run --rm -v "$PWD/data:/d" $IMAGE rm -rf /d/$DATA_SUB >/dev/null 2>&1
  rm -rf "$PROFILE"
}
trap cleanup EXIT

[ -x "$CHROME" ] || { echo "no chromium at $CHROME (set CHROME=)"; exit 2; }
mkdir -p "$SHOTS"

echo "== build"
docker compose -p $PROJ build -q >/dev/null || exit 1
docker run --rm -v "$PWD/data:/d" $IMAGE rm -rf /d/$DATA_SUB >/dev/null 2>&1

echo "== seed fixture"
docker run --rm -v "$PWD/data:/srv/data" -v "$PWD/tools:/srv/tools:ro" \
  -e DATA_DIR=/srv/data/$DATA_SUB -w /srv $IMAGE python -m tools.ui_fixture || exit 1

echo "== start isolated instance on :$PORT"
# 192.0.2.1 is TEST-NET-1: guaranteed unroutable, so the node reads offline.
PORT=$PORT DATA_DIR=/srv/data/$DATA_SUB COMFY_HOST=192.0.2.1 OLLAMA_URL=http://192.0.2.1:11434 \
  docker compose -p $PROJ up -d >/dev/null 2>&1 || exit 1
for _ in $(seq 1 30); do
  curl -sf http://127.0.0.1:$PORT/api/status >/dev/null && break
  sleep 1
done

"$CHROME" --headless=new --disable-gpu --no-sandbox --hide-scrollbars \
  --remote-debugging-port=$DEBUG_PORT --user-data-dir="$PROFILE" about:blank \
  >"$SHOTS/chrome.log" 2>&1 &
CHROME_PID=$!
for _ in $(seq 1 30); do
  curl -sf http://127.0.0.1:$DEBUG_PORT/json/version >/dev/null && break
  sleep 1
done

echo "== checks"
docker run --rm --network host -v "$PWD/tools:/srv/tools:ro" -v "$SHOTS:/shots" -w /srv $IMAGE \
  python tools/ui_check.py http://127.0.0.1:$DEBUG_PORT http://127.0.0.1:$PORT /shots
status=$?
echo "screenshots: $SHOTS"
exit $status
