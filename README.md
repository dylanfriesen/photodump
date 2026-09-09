# anime-forge

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

Loopback `:8096`, tailnet `:8452`. No public vhost — there is no auth.

## The two tasks

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
