# anime-forge — handoff

**Read this before touching anything.** The unusual parts of this codebase are
responses to hard environmental constraints, not stylistic choices, and several
look like over-engineering until you know why they exist.

---

## 1. What it is

A studio for an Instagram anime page. Three tasks:

- **Fuse** — blend two franchises into one original design (Pokémon × Jujutsu Kaisen)
- **Extend** — outpaint a photo onto a new IG canvas (landscape → 4:5, 9:16, 1:1)
- **Animate** — turn any generated still into a short clip

Plus caption/hashtag drafting, a reference-image library, and saved recipes.

---

## 2. The constraint that shapes everything

**The app and the GPU are on different machines, and the GPU is usually asleep.**

| | |
|---|---|
| `kanto` | Public IP `23.168.16.151`. 20 cores, 125GB RAM. **No usable GPU** — `card0` is a Matrox BMC. Always up. |
| `desktop-gqtd71t` | Windows. **AMD RX 9070 16GB.** Tailnet `100.109.223.93`. Awake only when Dylan is at it. |

Three consequences that are *not* negotiable:

1. **Wake-on-LAN is impossible.** kanto is not on the desktop's LAN; magic packets
   are layer-2 and do not cross the tailnet. Nothing here can wake that PC. Do not
   add a "wake the node" feature — it cannot work.
2. **The queue is the architecture, not a convenience.** Jobs are submitted from
   anywhere at any time and drain whenever the desktop happens to be up.
3. **An unreachable render node is a normal resting state, not an error.**

---

## 3. State: what is proven and what is not

**Proven** — `./tools/smoke.sh`, 13 assertions, currently 13/13. Runs with no GPU
and no desktop, against a mock ComfyUI in `tools/mock_comfy.py`.

**NOT proven — read this twice:**

> **No part of this has ever talked to a real ComfyUI.** The desktop was offline
> for the entire build. No model has ever loaded, no image has ever actually been
> generated. The graphs in `app/workflows/*.json` were written from documentation,
> **not** captured from a running instance.

That makes the workflow JSONs the highest-risk surface in the repo. Node class
names and socket indices are plausible but unverified. `wan_i2v.json` is the
least certain of the five — WAN's node naming has churned across releases.

**The first real task is almost certainly: stand up ComfyUI on the desktop and
correct the graphs against `/object_info`.** Expect to fix things. That is
expected, not a defect report.

---

## 4. Running it

```sh
docker compose up -d --build          # code is baked in; plain restart ships nothing
tailscale serve --bg --https=8452 http://127.0.0.1:8096
```

Loopback `:8096`, tailnet `:8452`. **There is no auth — it must never get a public
vhost.** (Contrast with dylanime/nihongo, which are public and gated by Caddy
`forward_auth`.)

Testing, no GPU required:

```sh
./tools/smoke.sh          # isolated project on :8097, leaves :8096 running
```

The mock can simulate the awkward states directly:

```sh
python tools/mock_comfy.py --flaky          # up, but crashes on every submit
python tools/mock_comfy.py --no-ipadapter   # node pack missing
python tools/mock_comfy.py --latency 60     # slow render, to interrupt
```

---

## 5. Invariants — do not "simplify" these

**a. `ComfyOffline` vs `ComfyError` is load-bearing.**
`ComfyOffline` = node unreachable → **requeue**. `ComfyError` = node rejected the
work → **fail**. Collapsing these into generic error handling destroys the whole
design: every job would fail the moment the PC slept. `app/comfy.py` wraps
transport exceptions as `ComfyOffline` deliberately.

**b. The requeue ceiling (`MAX_ATTEMPTS = 5`).**
A sleeping PC and a ComfyUI that crashes on one specific graph are
indistinguishable from here. Without the cap, the second case requeues forever.
Covered by smoke test 5.

**c. WAN frame count must be 4n+1.**
`comfy._build_video` rounds to it. WAN silently degrades the final chunk otherwise
— degrades, not errors, so nothing will tell you it broke.

**d. Outpaint canvas stays near 1.3MP.**
`app/imageops.prepare` downscales the source so the *padded* result lands there.
SDXL falls apart much above ~1MP and 16GB will OOM on a naive 1920×2404 canvas.
Covered by smoke test 2.

**e. Workflow JSON node IDs are API, not decoration.**
`comfy.py` indexes graphs by string key (`wf["3"]` is the KSampler, `wf["14"]` the
pad node, `wf["23"]` the WAN latent). Renumber a graph and the builder breaks
silently on a `KeyError` that surfaces as a requeue, not a clear failure.

---

## 6. Landmines

**fp8 is broken on RDNA4/Windows.** Documented as supported; `torch.mm`/`torch.mul`
raise `NotImplementedError` in practice ([ROCm issue #6019](https://github.com/ROCm/legacy-rocm-build/issues/6019)).
Nearly every published WAN workflow uses `umt5_xxl_fp8_e4m3fn_scaled.safetensors`
as its text encoder — **that exact file will fail on this hardware.** Use fp16 or
GGUF. This will be the first thing to bite anyone setting up video.

**WAN second-run slowdown.** [ComfyUI #12672](https://github.com/Comfy-Org/ComfyUI/issues/12672) —
WAN 2.2 i2v runs 4–5× slower on its second run under ROCm. Upstream, not ours.

**Container writes files as root.** Anything under `data/` is root-owned; host-side
`rm -rf` fails. Delete from inside a container (see `tools/smoke.sh` cleanup).

**No ffmpeg on kanto.** Needed before any reel-assembly work.

**`--build` is mandatory.** Code is baked into the image; `docker compose restart`
ships nothing. Same trap as nihongo.

---

## 7. Map

```
app/config.py     paths, aspect buckets, outpaint pad geometry, WAN model names
app/prompts.py    recipe -> danbooru-tag compiler; fusion modes and starters
app/comfy.py      ComfyUI HTTP client; graph builders; the Offline/Error split
app/imageops.py   outpaint source scaling
app/worker.py     queue drainer; requeue-vs-fail semantics
app/captions.py   Ollama caption/hashtag drafting
app/db.py         SQLite schema + additive migrations
app/main.py       FastAPI routes
app/workflows/    5 ComfyUI API-format graphs (txt2img, img2img, ipadapter,
                  outpaint, wan_i2v)
web/              single-page UI (no framework, no build step)
tools/            mock ComfyUI + smoke suite
```

**Why tags, not fine-tuning:** Illustrious/NoobAI-family SDXL checkpoints are
trained on danbooru tags and already know Gojo, Sukuna, Gardevoir by name. Fusion
is a tag-blending problem. Do not reach for LoRA training to solve it.

**Why captions run on Ollama on the desktop:** deliberately the same box as the
renderer, so captions inherit its availability envelope instead of introducing a
second thing that can be down independently.

---

## 8. Suggested work, in priority order

**P0 — Validate against real hardware.** Install ComfyUI Desktop for Windows
(v0.7.0+, ships ROCm 7.1.1 prepackaged — no ZLUDA, no DirectML). Launch with
`--listen 0.0.0.0 --port 8188`. Then reconcile each workflow JSON against
`GET /object_info`. Nothing else matters until renders actually come out.

**P1 — Reel assembler.** Take favourited images → Ken Burns motion → beat-synced
cuts → 9:16 export. Pure ffmpeg on kanto, so it works while the desktop sleeps.
Needs ffmpeg installed first. Design note: this belongs on kanto precisely
*because* it needs no GPU.

**P1 — Deploy OpenCut** for CapCut-style hand-finishing. Deliberately not built
here: rebuilding a mature NLE is not a good use of effort. anime-forge produces
and auto-assembles; a real editor finishes.

**P2 — Batch seed variation** (same prompt, n seeds, contact sheet), **LoRA
stacking**, **ControlNet pose** from a reference.

**P3 — Instagram Graph API posting.** Needs a Business/Creator account linked to a
Facebook Page plus app review. Meaningful setup cost before a single post; was
explicitly deferred.

---

## 9. Open questions for Dylan

1. Which checkpoint? `.env` defaults to `waiNSFWIllustrious_v140.safetensors` as a
   placeholder — it must match a real filename in `ComfyUI/models/checkpoints/`.
2. House style — the starters in `app/prompts.py` are guesses at taste, not a brief.
3. Is 5B WAN enough, or is 14B-GGUF worth the setup pain and slower renders?
