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

if [ "${1:-}" != "--progress-only" ]; then
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

fi

say "8. live progress reaches the UI mid-render"
# A slow render so there is a window to observe. The mock streams the same
# per-step websocket messages ComfyUI does, addressed to the client id that
# submitted - which is the part most likely to break.
start_mock --latency 20
curl -s -X POST $B/api/generate -H 'Content-Type: application/json' \
  -d '{"subject_a":"eevee","subject_b":"toji","mode":"design_fusion","count":1,"steps":20}' >/dev/null
SRC=""; PCT=0; STEPS=0
for _ in $(seq 1 20); do
  sleep 2
  read -r SRC PCT STEPS <<<"$(curl -s $B/api/status | python3 -c "
import json,sys
p = json.load(sys.stdin).get('progress') or {}
print(p.get('source','-'), p.get('percent') or 0, p.get('steps') or 0)")"
  [ "$SRC" = "steps" ] && break
done
check "progress source is the node's own step count" "$SRC" "steps"
check "step total matches the requested steps" "$STEPS" "20"
python3 -c "import sys; sys.exit(0 if 0 < float('$PCT') <= 92 else 1)" \
  && ok "percent is within the sampler band (${PCT}%)" \
  || bad "percent out of range: $PCT"
wait_for "jobs[0]['status']=='done'" 90 >/dev/null
check "progress clears when the job finishes" \
  "$(curl -s $B/api/status | field "d['progress']")" "None"

say "9. a job interrupted by a restart is recovered, not orphaned"
start_mock --latency 60
curl -s -X POST $B/api/generate -H 'Content-Type: application/json' \
  -d '{"subject_a":"mimikyu","subject_b":"nanami","mode":"design_fusion","count":1}' >/dev/null
wait_for "jobs[0]['status']=='running'" 45 \
  && ok "job is running before the restart" || bad "job never started"
PID_BEFORE=$(curl -s $B/api/jobs | field "d[0]['prompt_id']")
docker restart ${CONTAINER_NAME} >/dev/null 2>&1
sleep 8
# Recovery requeues it; the mock still holds the prompt, so it re-attaches
# rather than rendering the same job twice.
check "restart keeps the original node prompt" \
  "$(curl -s $B/api/jobs | field "d[0]['prompt_id']")" "$PID_BEFORE"
wait_for "jobs[0]['status']=='done'" 120 \
  && ok "recovered and completed after the restart" || bad "orphaned by the restart"
check "one image per job, no duplicate render" \
  "$(curl -s $B/api/jobs | python3 -c "
import json,sys
jid = json.load(sys.stdin)[0]['id']
import urllib.request
imgs = json.load(urllib.request.urlopen('$B/api/images'))
print(sum(1 for i in imgs if i['job_id']==jid))")" "1"

say "10. stop cancels a queued job outright"
start_mock --latency 60
curl -s -X POST $B/api/generate -H 'Content-Type: application/json' \
  -d '{"subject_a":"mew","subject_b":"gojo","mode":"design_fusion","count":1}' >/dev/null
curl -s -X POST $B/api/generate -H 'Content-Type: application/json' \
  -d '{"subject_a":"mew","subject_b":"nanami","mode":"design_fusion","count":1}' >/dev/null
# The worker idles for POLL_IDLE (20s) between sweeps, so a freshly queued job
# is not running yet. Wait for the claim rather than assuming it has happened.
wait_for "[j for j in jobs if j['status']=='running']" 45 \
  && ok "a job is rendering" || bad "nothing started rendering"
QID=$(curl -s $B/api/jobs | field "[j['id'] for j in d if j['status']=='queued'][0]")
curl -s -X POST $B/api/jobs/$QID/stop >/dev/null
check "queued job is cancelled" \
  "$(curl -s $B/api/jobs | field "[j['status'] for j in d if j['id']==$QID][0]")" "cancelled"

say "11. pause a RUNNING job -> paused, and the attempt is given back"
RID=$(curl -s $B/api/jobs | field "[j['id'] for j in d if j['status']=='running'][0]")
curl -s -X POST $B/api/jobs/$RID/pause >/dev/null
wait_for "[j for j in jobs if j['id']==$RID and j['status']=='paused']" 45 \
  && ok "running job reached paused" || bad "running job never paused"
# MAX_ATTEMPTS is 5; pausing must not spend one, or five pauses kill the job.
check "pause did not burn an attempt" \
  "$(curl -s $B/api/jobs | field "[j['attempts'] for j in d if j['id']==$RID][0]")" "0"
check "paused jobs are not counted as queued" \
  "$(curl -s $B/api/status | field "d['paused_jobs']")" "1"

say "12. resume returns it to the queue and it completes"
start_mock --latency 2
curl -s -X POST $B/api/jobs/$RID/resume >/dev/null
wait_for "[j for j in jobs if j['id']==$RID and j['status']=='done']" 120 \
  && ok "resumed job completed" || bad "resumed job did not complete"

say "13. a scheduled job is held until its time"
FUTURE=$(python3 -c "import datetime;print((datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(seconds=45)).strftime('%Y-%m-%d %H:%M:%S'))")
curl -s -X POST $B/api/generate -H 'Content-Type: application/json' \
  -d '{"subject_a":"eevee","subject_b":"nobara","mode":"design_fusion","count":1}' >/dev/null
sleep 2
SID=$(curl -s $B/api/jobs | field "d[0]['id']")
curl -s -X POST $B/api/jobs/$SID/schedule -H 'Content-Type: application/json' \
  -d "{\"not_before\":\"$FUTURE\"}" >/dev/null
sleep 15
check "scheduled job has not started" \
  "$(curl -s $B/api/jobs | field "[j['status'] for j in d if j['id']==$SID][0]")" "queued"
check "scheduled work is not reported as due" \
  "$(curl -s $B/api/status | field "d['scheduled']")" "1"
wait_for "[j for j in jobs if j['id']==$SID and j['status']=='done']" 120 \
  && ok "ran once its time arrived" || bad "never ran after its scheduled time"

say "14. pausing the QUEUE stops dispatch without touching the job"
curl -s -X POST $B/api/queue/pause >/dev/null
curl -s -X POST $B/api/generate -H 'Content-Type: application/json' \
  -d '{"subject_a":"snorlax","subject_b":"megumi","mode":"design_fusion","count":1}' >/dev/null
sleep 20
PID_=$(curl -s $B/api/jobs | field "d[0]['id']")
check "job stays queued while the queue is paused" \
  "$(curl -s $B/api/jobs | field "[j['status'] for j in d if j['id']==$PID_][0]")" "queued"
check "status reports the queue as paused" \
  "$(curl -s $B/api/status | field "str(d['queue_paused'])")" "True"
curl -s -X POST $B/api/queue/resume >/dev/null
wait_for "[j for j in jobs if j['id']==$PID_ and j['status']=='done']" 90 \
  && ok "drained once the queue resumed" || bad "did not drain after resume"

say "15. style match chain: cleaned reference, pass-2 tags, upscale on pass 2 only"
start_mock --latency 2
docker run --rm -v /tmp:/t ${PROJ}-photodump python -c "
from PIL import Image, ImageDraw
im = Image.new('RGB', (680, 856), (20, 20, 24))
ImageDraw.Draw(im).rectangle((30, 100, 110, 700), fill=(240, 240, 240))
im.save('/t/smoke_style.png')" >/dev/null 2>&1
SREF=$(curl -s -X POST $B/api/refs -F "file=@/tmp/smoke_style.png" -F "label=style" -F "kind=style" | field "d['id']")
check "fill preview is the reference's size" \
  "$(curl -s -X POST $B/api/refs/$SREF/clean-preview -H 'Content-Type: application/json' \
      -d '{"regions":[[0.03,0.1,0.15,0.75]]}' | python3 -c "
import sys, struct; b = sys.stdin.buffer.read(); print('%dx%d' % struct.unpack('>II', b[16:24]))")" "680x856"
curl -s -X POST $B/api/generate -H 'Content-Type: application/json' -d "{
  \"prompt\":\"cynthia\",\"quality\":false,\"negative_full\":\"text\",\"workflow\":\"img2img\",
  \"ref_ids\":[$SREF],\"denoise\":0.45,\"count\":1,\"second_pass\":true,\"hires\":true,
  \"second_pass_prompt_add\":\"platinum blonde hair\",\"second_pass_negative_add\":\"dark hair\",
  \"clean_regions\":[[0.03,0.1,0.15,0.75]]}" >/dev/null
wait_for "len([j for j in jobs if 'platinum blonde' in j['prompt'] and j['status']=='done'])==1" 120 \
  && ok "pass 2 was chained and completed" || bad "pass 2 never completed"
{ read -r P1; read -r P2; } < <(curl -s $B/api/jobs | python3 -c "
import json,sys
jobs = json.load(sys.stdin)
p2 = [j for j in jobs if 'platinum blonde' in j['prompt']][0]
p1 = [j for j in jobs if 'platinum blonde hair' in j['params']][0]
a, b = json.loads(p1['params']), json.loads(p2['params'])
print('%s|%s|%s' % (p1['prompt'], bool(a.get('clean_boxes')), a.get('hires')))
print('%s|%s|%s|%s' % (p2['negative'], 'clean_regions' in b, b.get('hires'), p2['src_image_id'] is not None))")
check "pass 1: original prompt, reference cleaned, hires deferred" "$P1" "cynthia|True|True"
check "pass 2: negative extended, no re-clean, hires on, sourced from pass 1" "$P2" "text, dark hair|False|True|True"

say "16. a still delivers as an exact-size JPEG with the node DOWN"
docker kill mock-comfy >/dev/null 2>&1
STILL=$(curl -s $B/api/images | field "[i['id'] for i in d if i['filename'].endswith('.png')][0]")
curl -s -X POST $B/api/images/$STILL/deliver -H 'Content-Type: application/json' -d '{"target":"feed"}' >/dev/null
wait_for "any(j['status']=='done' and 'feed encode of #$STILL' in j['prompt'] for j in jobs)" 60 \
  && ok "still delivery ran locally" || bad "still delivery did not run"
DJ=$(curl -s $B/api/jobs | field "[j['id'] for j in d if 'feed encode of #$STILL' in j['prompt']][0]")
check "delivered still is a 1080x1350 JPEG" \
  "$(docker exec ${CONTAINER_NAME} python -c "
from PIL import Image; im = Image.open('/srv/data/_smoke/out/${DJ}_ig.jpg'); print(im.format, '%dx%d' % im.size)")" "JPEG 1080x1350"

say "17. video settings without a video workflow are refused, not rendered as a still"
check "LTX params with no workflow -> 400" \
  "$(curl -s -o /dev/null -w '%{http_code}' -X POST $B/api/generate -H 'Content-Type: application/json' \
      -d "{\"prompt\":\"x\",\"ref_ids\":[$SREF],\"video_backend\":\"ltx\",\"video_size\":\"story_hd\"}")" "400"

say "18. LoRAs: an installed one renders through LoraLoader, an unknown one fails fast"
start_mock --latency 1
for _ in $(seq 1 12); do
  [ "$(curl -s $B/api/status | field "d['online']")" = "True" ] && break
  sleep 3
done
check "node list includes LoRAs (new COMBO schema)" \
  "$(curl -s $B/api/node | field "','.join(d.get('loras', []))")" "zzz/evelyn_chevalier_il.safetensors,retro_cel_style.safetensors"
curl -s -X POST $B/api/generate -H 'Content-Type: application/json' \
  -d '{"prompt":"lora smoke ok","loras":[{"name":"retro_cel_style.safetensors","strength":0.7}]}' >/dev/null
curl -s -X POST $B/api/generate -H 'Content-Type: application/json' \
  -d '{"prompt":"lora smoke missing","loras":[{"name":"not_installed.safetensors","strength":1}]}' >/dev/null
wait_for "any(j['status']=='done' and j['prompt'].endswith('lora smoke ok') for j in jobs) and any(j['status']=='failed' and j['prompt'].endswith('lora smoke missing') for j in jobs)" 90 \
  && ok "installed LoRA rendered, unknown LoRA failed" || bad "LoRA jobs did not settle as expected"
check "unknown LoRA failed on the first attempt, not requeued" \
  "$(curl -s $B/api/jobs | field "[j['attempts'] for j in d if j['prompt'].endswith('lora smoke missing')][0]")" "1"
check "mock saw the LoRA wired into the sampler" \
  "$(docker logs mock-comfy 2>&1 | grep -c "loras=retro_cel_style.safetensors sampler_model=\['40', 0\]")" "1"

printf '\n\033[1m%d passed, %d failed\033[0m\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
