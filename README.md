# photodump

Original anime mashup art for Instagram, rendered on the desktop GPU.

Fuse two franchises into one design (Pokémon × Jujutsu Kaisen and friends),
extend photos onto new canvases, and draft captions — all queued from kanto and
rendered on `desktop-gqtd71t`.

## Why it is split across two machines

kanto has no usable GPU (`card0` is the Matrox BMC). The desktop has an
**RX 9070 16GB**, but it is a desktop: awake when you're at it, asleep otherwise.
And because kanto sits on a public IP rather than your LAN, **Wake-on-LAN cannot
reach it** — magic packets are layer-2 and do not cross the tailnet.

So the queue is the whole design:

```
  kanto (always up)                      desktop-gqtd71t (intermittent)
  ┌──────────────────────┐               ┌──────────────────────────┐
  │ FastAPI + SQLite     │               │ ComfyUI  :8188           │
  │ refs / recipes / jobs│  tailnet      │   RX 9070 16GB, ROCm     │
  │ worker  ────────────────────────────▶│ Ollama   :11434          │
  │ :8096 loopback       │               │   (captions)             │
  └──────────────────────┘               └──────────────────────────┘
```

Queue from your phone whenever. Jobs sit in `queued` while the PC sleeps and
drain the moment it wakes. **A render node that disappears mid-job is never a
failure** — the job is requeued, not failed. Only a malformed graph or a missing
checkpoint fails a job.

## Setup on the desktop (one time)

1. Install **ComfyUI Desktop for Windows** (v0.7.0+). It ships with AMD ROCm
   7.1.1 prepackaged — no ZLUDA, no DirectML, no dual-boot.
2. Start it with the API reachable over the tailnet:
   `--listen 0.0.0.0 --port 8188`
3. Drop an Illustrious/NoobAI-family SDXL checkpoint into
   `ComfyUI/models/checkpoints/` and set `CHECKPOINT` in `.env` to the exact filename.
4. Optional, for the IP-Adapter workflow: install the
   `ComfyUI_IPAdapter_plus` node pack. The UI greys the option out until it's present.

**Avoid fp8-quantised checkpoints.** FP8 (e4m3fn) operators are broken on
RDNA4/Windows — documented as supported, but `torch.mm`/`torch.mul` raise
`NotImplementedError`. Use fp16 (SDXL) or GGUF quants. 16GB is plenty for SDXL.

## Running

```sh
docker compose up -d --build      # code is baked into the image
tailscale serve --bg --https=8452 http://127.0.0.1:8096
```

Loopback `:8096`, live at **https://kanto.tail4f3755.ts.net:8452**.
No public vhost — there is no auth.

## The tasks

**Fuse** — blends two sources into one design. Four modes:

| mode | what it does |
|---|---|
| `design_fusion` | one character redesigned in the other's visual language (the workhorse) |
| `crossover_scene` | both characters together in one shot |
| `style_transfer` | subject A rendered inside B's world |
| `original_oc` | a new character carrying DNA from both |

Illustrious/NoobAI checkpoints are trained on danbooru tags, so they *already
know* Gojo, Sukuna, Gardevoir by name. Fusion is tag blending, not fine-tuning.

**Extend** — outpaints a photo onto a new canvas (landscape → 4:5, 9:16, 1:1).
Your original pixels are never cropped; only new territory is invented. The
source is auto-downscaled so the *padded* canvas lands near 1.3MP — SDXL falls
apart much above that, and a 16GB card will OOM trying.

Describe the **finished scene**, not just the new parts.

## Reference images

| workflow | what it does | needs |
|---|---|---|
| `img2img` | keeps the composition, restyles it (denoise is the dial) | stock nodes |
| `ipadapter` | borrows the look, invents the composition | `ComfyUI_IPAdapter_plus` |
| `outpaint` | extends the canvas | stock nodes |

## Captions

Drafted by Ollama on the same desktop — deliberately the same box as the
renderer, so captions inherit its availability envelope instead of adding a
second thing that can be down.

## Layout

```
app/config.py     paths, aspect buckets, outpaint pad geometry
app/prompts.py    recipe -> danbooru-tag compiler, fusion modes, starters
app/comfy.py      ComfyUI HTTP client (ComfyOffline vs ComfyError)
app/imageops.py   source prep for outpainting
app/worker.py     queue drainer
app/captions.py   Ollama caption/hashtag drafting
app/workflows/    ComfyUI API-format graphs
web/              single-page UI
```

## Testing without the desktop

`ComfyOffline` vs `ComfyError` is the important distinction and is easy to get
wrong. The full pipeline was verified against a mock ComfyUI implementing
`/system_stats`, `/object_info`, `/prompt`, `/history`, `/view` and
`/upload/image` — including killing the mock mid-render to confirm jobs requeue
rather than fail.

## Video

**Animate** turns any generated still into a clip — open it in the gallery,
hit Animate, describe the *motion* only.

Runs WAN 2.2 **TI2V-5B** (single-model; the 14B needs dual high/low-noise
models and GGUF quantisation to fit 16GB). Set the model filenames in `.env`:

```
WAN_UNET=wan2.2_ti2v_5B_fp16.safetensors
WAN_CLIP=umt5_xxl_fp16.safetensors     # NOT the fp8 one - see below
WAN_VAE=wan2.2_vae.safetensors
```

**The fp8 trap bites hardest here.** Nearly every published WAN workflow uses
`umt5_xxl_fp8_e4m3fn_scaled.safetensors` as the text encoder — precisely the
format broken on RDNA4/Windows. Use the fp16 or a GGUF encoder or it will fail
with `NotImplementedError` on an operator the docs claim is supported.

Expect minutes per clip, not seconds. Also note ComfyUI issue #12672: WAN 2.2
i2v runs 4-5x slower on its *second* run under ROCm.

Frame count is forced to 4n+1 — WAN silently degrades the final chunk otherwise.

## Reels

**Reel** assembles selected stills into a 1080x1920 mp4: Ken Burns motion, cuts
on the beat, optional music.

This is the one task that runs **on kanto, not the render node** - it is ffmpeg
on CPU, so reels build while the desktop is asleep. The worker claims reel jobs
without probing the node at all.

Cut timing is driven by an explicit **BPM** rather than onset detection. Real
beat detection means librosa (numpy/scipy/numba) for something you already know,
and hard cuts on the beat are what reads as "synced" anyway - a crossfade softens
exactly the moment you were trying to hit. Crossfades are available and overlap,
so they shorten the finished reel; `reels.total_seconds()` accounts for that and
the UI mirrors it.

Upload music under References with kind `audio`.

**Shots can come from uploaded photos, not just generated stills.** The Reel
panel's *shots from* control switches the gallery between `renders`,
`my photos` (image references you uploaded) and `both`. Since reels build on
kanto, a reel made entirely from uploads needs **no GPU at all** — it works
with the desktop switched off. Shots are addressed as `{src, id}` so the two
sources can be mixed in any order.

### Ken Burns gotcha

`zoompan`'s `d` is how many output frames each *input* frame becomes. With a
looped input, `d=frames` multiplies out to `frames x fps x seconds` of encoding -
minutes of work for a few seconds of video. Use `d=1` and drive motion from `on`,
bounding the shot with `-frames:v`. This was a real bug here, not a hypothetical.

## Editing

Not built here, deliberately. For CapCut-style hand-finishing use **OpenCut**
(self-hosted, browser-based, multi-track timeline) or DaVinci Resolve on the
desktop. photodump's job is to produce and auto-assemble assets; a mature NLE
is where they get finished.

## Failure semantics

Three distinct outcomes, and the distinction is the point:

| situation | outcome |
|---|---|
| node asleep / disappears mid-render | requeued, **not** failed |
| node crashes on this graph repeatedly | failed after 5 attempts (`MAX_ATTEMPTS`) |
| malformed graph, missing checkpoint | failed immediately with the node's own error |

All three are covered by the mock-based tests described above.
