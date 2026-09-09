#!/usr/bin/env bash
# End-to-end test against a mock ComfyUI. Needs no GPU and no desktop.
#
# Runs as its own compose project on :8097 with an isolated data dir, so the
# live instance on :8096 keeps running untouched.
#
#   ./tools/smoke.sh
#
# Exits non-zero on the first failed assertion.
set -uo pipefail
cd "$(dirname "$0")/.."

PROJ=photodump-smoke
export CONTAINER_NAME=photodump-smoke
NET=${PROJ}_default
B=http://127.0.0.1:8097
PASS=0; FAIL=0

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$*"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAIL=$((FAIL+1)); }
check(){ [ "$2" = "$3" ] && ok "$1 ($3)" || bad "$1: expected '$3', got '$2'"; }

cleanup() {
  docker rm -f mock-comfy >/dev/null 2>&1
  PORT=8097 DATA_DIR=/srv/data/_smoke docker compose -p $PROJ down >/dev/null 2>&1
  # Files are container-owned (root); remove them from inside a container.
  docker run --rm -v "$PWD/data:/d" ${PROJ}-photodump rm -rf /d/_smoke >/dev/null 2>&1 \
    || rm -rf data/_smoke 2>/dev/null
}
trap cleanup EXIT

start_mock() {  # $@ = extra mock flags
  docker rm -f mock-comfy >/dev/null 2>&1
  docker run -d --rm --name mock-comfy --network "$NET" \
    -v "$PWD/tools:/m:ro" ${PROJ}-photodump python /m/mock_comfy.py "$@" >/dev/null
  sleep 2
}

# jq-free JSON field read
field() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }

wait_for() {  # $1 = python expr over jobs list -> truthy, $2 = timeout secs
  local expr=$1 limit=${2:-90} waited=0
  while [ $waited -lt "$limit" ]; do
    sleep 3; waited=$((waited+3))
    if curl -s $B/api/jobs | python3 -c "
import json,sys
jobs=json.load(sys.stdin)
sys.exit(0 if ($expr) else 1)" 2>/dev/null; then return 0; fi
  done
  return 1
}

say "build + start (isolated project on :8097)"
docker compose -p $PROJ build -q >/dev/null 2>&1
PORT=8097 DATA_DIR=/srv/data/_smoke COMFY_HOST=mock-comfy \
  CHECKPOINT=mock_illustrious.safetensors \
  docker compose -p $PROJ up -d >/dev/null 2>&1
sleep 5
start_mock --latency 2
# The worker backs off for POLL_IDLE after a failed probe, so give it a beat
# rather than racing it - this is test timing, not app behaviour.
for _ in $(seq 1 12); do
  [ "$(curl -s $B/api/status | field "d['online']")" = "True" ] && break
  sleep 3
done
check "node reports online" "$(curl -s $B/api/status | field "d['online']")" "True"

say "1. text-to-image"
curl -s -X POST $B/api/generate -H 'Content-Type: application/json' \
  -d '{"subject_a":"gardevoir","subject_b":"gojo satoru","mode":"design_fusion","count":2}' >/dev/null
wait_for "sum(1 for j in jobs if j['status']=='done')==2" 90 \
  && ok "both stills rendered" || bad "stills did not complete"
check "images stored" "$(curl -s $B/api/images | field "len(d)")" "2"

say "2. outpaint geometry (1920x1080 -> 4:5)"
docker run --rm -v /tmp:/t ${PROJ}-photodump python -c "
from PIL import Image; Image.new('RGB',(1920,1080),(90,150,210)).save('/t/smoke_land.png')" >/dev/null 2>&1
RID=$(curl -s -X POST $B/api/refs -F "file=@/tmp/smoke_land.png" -F "label=land" | field "d['id']")
curl -s -X POST $B/api/generate -H 'Content-Type: application/json' \
  -d "{\"prompt\":\"a valley\",\"workflow\":\"outpaint\",\"ref_id\":$RID,\"count\":1,\"extend_target\":\"portrait\"}" >/dev/null
wait_for "any(j['status']=='done' and 'outpaint' in j['params'] for j in jobs)" 90 \
  && ok "outpaint completed" || bad "outpaint did not complete"
MP=$(curl -s $B/api/jobs | python3 -c "
import json,sys
j=[x for x in json.load(sys.stdin) if 'outpaint' in x['params']][0]
p=json.loads(j['params'])['pads']; w,h=p['final_size']
print('%.1f' % (w*h/1e6)); ")
check "final canvas near 1.3MP" "$MP" "1.3"
RATIO=$(curl -s $B/api/jobs | python3 -c "
import json,sys
j=[x for x in json.load(sys.stdin) if 'outpaint' in x['params']][0]
w,h=json.loads(j['params'])['pads']['final_size']; print('%.2f' % (w/h))")
check "aspect is 4:5" "$RATIO" "0.80"

say "3. image-to-video (WAN)"
IMG=$(curl -s $B/api/images | field "[i['id'] for i in d if i['filename'].endswith('.png')][0]")
curl -s -X POST $B/api/generate -H 'Content-Type: application/json' \
  -d "{\"prompt\":\"hair drifting\",\"workflow\":\"wan_i2v\",\"src_image_id\":$IMG,\"count\":1,\"seconds\":3}" >/dev/null
wait_for "any(j['status']=='done' and 'wan_i2v' in j['params'] for j in jobs)" 90 \
  && ok "clip rendered" || bad "clip did not complete"
check "webm stored" \
  "$(curl -s $B/api/images | field "sum(1 for i in d if i['filename'].endswith('.webm'))")" "1"

say "4. node sleeps mid-render -> REQUEUE, not fail"
start_mock --latency 60          # long render so we can interrupt it
curl -s -X POST $B/api/generate -H 'Content-Type: application/json' \
  -d '{"subject_a":"gengar","subject_b":"cursed spirit","mode":"design_fusion","count":1}' >/dev/null
sleep 8
docker kill mock-comfy >/dev/null 2>&1   # the PC goes to sleep
sleep 30
LAST=$(curl -s $B/api/jobs | field "d[0]['status']")
check "job requeued rather than failed" "$LAST" "queued"
start_mock --latency 2                    # PC wakes
wait_for "jobs[0]['status']=='done'" 90 && ok "drained on wake" || bad "did not drain on wake"

say "5. node UP but crashing -> fail after MAX_ATTEMPTS"
start_mock --flaky
curl -s -X POST $B/api/generate -H 'Content-Type: application/json' \
  -d '{"subject_a":"pikachu","subject_b":"sukuna","mode":"design_fusion","count":1}' >/dev/null
wait_for "jobs[0]['status']=='failed'" 200 \
  && ok "gave up instead of looping forever" || bad "did not fail within 200s"
check "attempt ceiling honoured" "$(curl -s $B/api/jobs | field "d[0]['attempts']")" "5"

say "6. reel assembly (renders locally - no node needed)"
IDS=$(curl -s $B/api/images | python3 -c "
import json,sys; print(json.dumps([i['id'] for i in json.load(sys.stdin) if i['filename'].endswith('.png')]))")
docker kill mock-comfy >/dev/null 2>&1     # prove reels drain with the node DOWN
sleep 2
curl -s -X POST $B/api/reels -H 'Content-Type: application/json' \
  -d "{\"image_ids\":$IDS,\"bpm\":128,\"beats_per_shot\":4,\"transition\":\"cut\"}" >/dev/null
wait_for "any(j['status']=='done' and 'reel' in j['params'] for j in jobs)" 150 \
  && ok "reel built with the render node offline" || bad "reel did not build"
RJ=$(curl -s $B/api/jobs | field "[j['id'] for j in d if 'reel' in j['params']][0]")
DUR=$(docker exec ${CONTAINER_NAME} ffprobe -v error -show_entries format=duration \
  -of csv=p=0 /srv/data/_smoke/out/${RJ}_reel.mp4 2>/dev/null | cut -c1-4)
check "reel duration matches 4 shots x 1.875s" "$DUR" "7.46"
check "reel is 1080x1920" \
  "$(docker exec ${CONTAINER_NAME} ffprobe -v error -select_streams v -show_entries stream=width,height -of csv=p=0 /srv/data/_smoke/out/${RJ}_reel.mp4 2>/dev/null)" "1080,1920"
start_mock --latency 2

say "7. IP-Adapter hidden when the node pack is absent"
start_mock --no-ipadapter
check "has_ipadapter reported false" "$(curl -s $B/api/node | field "d['has_ipadapter']")" "False"

printf '\n\033[1m%d passed, %d failed\033[0m\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
