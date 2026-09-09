# photodump — brief for the desktop session

You are running on **desktop-gqtd71t** (Windows, Radeon **RX 9070 16GB**). This
machine is the **render node** for an app that lives on another machine.

Everything on the other machine is built and tested. **Your job is to make this
machine render, and to correct the workflow graphs that have never met a real
ComfyUI.** That is the whole task. Do not rebuild or redesign the app.

---

## 1. The setup, in one paragraph

`photodump` is a studio for making original anime mashup art. It runs on
**kanto** (Linux, always on, tailnet `100.97.103.85`) which has **no usable
GPU**. You have the GPU. kanto queues jobs and drains them over the tailnet
whenever you are awake — so a sleeping desktop is normal, not an error.

The app is reachable from this machine at:

```
https://kanto.tail4f3755.ts.net:8452
```

That URL is also its API. You will use it to check your own work.

## 2. What already works — do not touch

- The whole web app, desktop and mobile layouts
- The job queue, including requeue-on-sleep and a 5-attempt failure ceiling
- Reel assembly (runs on kanto with ffmpeg — needs no GPU)
- A preflight checker that will tell you exactly what this machine is missing
- A 16-assertion test suite on kanto that passes against a mock

**Nothing has ever talked to a real ComfyUI.** The five workflow graphs were
written from documentation. Expect them to be wrong in places. Finding and
fixing that is the point of this session, not a regression.

---

## 3. Do these in order

### 3.1 Install ComfyUI

**ComfyUI Desktop for Windows, v0.7.0 or later.** It ships with **AMD ROCm
7.1.1 prepackaged** and the RX 9070 (gfx1201 / Navi 48) is officially
supported.

> You do **not** need ZLUDA, DirectML, WSL, or a Linux dual-boot. Any guide
> telling you otherwise predates official support. Do not go down that road.

### 3.2 Make it reachable from kanto

ComfyUI binds localhost by default, which the tailnet cannot reach. Add to the
launch arguments (Settings → Server → extra launch arguments):

```
--listen 0.0.0.0 --port 8188
```

Verify locally first, then confirm kanto can see it:

```powershell
curl http://127.0.0.1:8188/system_stats
```

Then from this machine, ask the app on kanto whether it can see you:

```powershell
curl https://kanto.tail4f3755.ts.net:8452/api/status
```

`"online": true` means the tailnet path works. If it stays false while your
local curl succeeds, it is almost always **Windows Firewall** — allow ComfyUI
on private networks.

### 3.3 Get a checkpoint

Download an **Illustrious-XL** or **NoobAI-XL** family SDXL checkpoint (~6.5GB)
into `ComfyUI/models/checkpoints/`.

These are trained on danbooru tags and already know Gojo, Sukuna, Gardevoir by
name — the app blends characters with tags, not LoRAs. Do not go looking for
character LoRAs.

**Never an fp8 checkpoint.** See §5.

### 3.4 Tell kanto the real filename

On **kanto**, `/home/dylan/photodump/.env` currently says:

```
CHECKPOINT=waiNSFWIllustrious_v140.safetensors
```

That is a placeholder. It must match your downloaded file **character for
character**. See §6 for how to apply this if you cannot reach kanto's shell.

After changing it: `cd /home/dylan/photodump && docker compose up -d`
(no rebuild needed for `.env` changes).

### 3.5 Read preflight — this is your worklist

```powershell
curl https://kanto.tail4f3755.ts.net:8452/api/preflight
```

It walks all five workflows and returns, per workflow, either `ok` or the
**missing ComfyUI node class** and/or the **model filename it wanted and could
not find**. This is your task list. It is also visible in the UI under the
render-node card.

Expect **Fuse** and **Extend** to go green first. IP-Adapter and Animate need
the extra installs in §7.

### 3.6 Reconcile the graphs

This is the real work.

> **Do this first — it is far faster than debugging my graphs.**
> ComfyUI ships official workflow templates for WAN 2.2 and LTX-2.5. Open the
> matching template in the ComfyUI UI, then **Workflow → Export (API Format)**.
> That gives you a graph whose node classes and socket indices are correct *for
> the version you actually installed*, straight from the vendor.
>
> Then reshape it to kanto's expectations rather than the other way round:
> keep the **node ids** listed in §4 (`comfy.py` indexes by string id), and keep
> the same loaders/inputs the builder writes into. Diff the exported template
> against §4 and you have your answer in minutes.
>
> The graphs in the repo were written from documentation. The exported template
> is ground truth. Prefer it.

Get the node's own capability map:

```powershell
curl http://127.0.0.1:8188/object_info > object_info.json
```

Then compare against the graphs in §4. For each mismatch, determine the
**correct class name and socket indices** on this ComfyUI version.

Two failure shapes, and they look different:

- **Wrong class name** → preflight names it directly. Easy.
- **Right class, wrong socket index or input key** → preflight passes but the
  job fails when it runs, with ComfyUI's own error in the Queue tab. Read that
  error; it is specific.

### 3.7 Prove it with a real render

From the UI, or:

```powershell
curl -X POST https://kanto.tail4f3755.ts.net:8452/api/generate `
  -H "Content-Type: application/json" `
  -d '{\"subject_a\":\"gardevoir\",\"subject_b\":\"gojo satoru\",\"mode\":\"design_fusion\",\"count\":1}'
```

Watch it in the Queue tab. A finished image appears in Gallery. **Until an
actual image comes out, nothing is verified.**

### 3.8 Ollama, for captions

Captions run on this machine too, deliberately — same availability as the
renderer.

```powershell
$env:OLLAMA_HOST="0.0.0.0:11434"; ollama serve
ollama pull qwen2.5:14b-instruct
```

kanto already points at `http://100.109.223.93:11434`.

---

## 4. The five graphs, as they stand

kanto's `comfy.py` builds these by **string node id** — `wf["3"]` is the
sampler, `wf["14"]` the pad node, `wf["23"]` the WAN latent. **Renumbering a
graph breaks the builder.** Change class names and inputs; keep the ids.

| id | txt2img | img2img | outpaint | ipadapter | wan_i2v |
|---|---|---|---|---|---|
| 3 | KSampler | KSampler | KSampler | KSampler | KSampler |
| 4 | CheckpointLoaderSimple | ← | ← | ← | — |
| 5 | EmptyLatentImage | — | — | EmptyLatentImage | — |
| 6 / 7 | CLIPTextEncode ×2 | ← | ← | ← | ← |
| 8 | VAEDecode | ← | ← | ← | ← |
| 9 | SaveImage | ← | ← | ← | — |
| 10 | — | LoadImage | LoadImage | LoadImage | LoadImage |
| 11 | — | VAEEncode | VAEEncodeForInpaint | — | — |
| 12 / 13 | — | — | — | IPAdapterUnifiedLoader, IPAdapterAdvanced | — |
| 14 | — | — | ImagePadForOutpaint | — | — |
| 20 / 21 / 22 | — | — | — | — | UNETLoader, CLIPLoader (`type: wan`), VAELoader |
| 23 | — | — | — | — | Wan22ImageToVideoLatent |
| 24 | — | — | — | — | SaveWEBM |

**Least certain:** `wan_i2v` — `Wan22ImageToVideoLatent`, `SaveWEBM`, and
`CLIPLoader` with `type: "wan"` have all churned across ComfyUI releases. Check
these against `object_info.json` before assuming.

Also constrained: WAN frame count must be **4n+1**. kanto already rounds to
that; if you change the latent node, keep it true or the last chunk silently
degrades.

---

## 5. The fp8 trap — read before downloading anything

**FP8 (e4m3fn) is broken on RDNA4 under Windows.** It is *documented* as
supported, but `torch.mm` / `torch.mul` raise `NotImplementedError` in practice.

Nearly every published WAN workflow uses
`umt5_xxl_fp8_e4m3fn_scaled.safetensors` as its text encoder. **That exact file
will fail on this card.** Take the **fp16** or a **GGUF** quant instead. Same
rule for checkpoints.

If you see `NotImplementedError` on a basic tensor op, this is why — it is not
a bug in the app.

Also expect **ComfyUI issue #12672**: WAN 2.2 image-to-video runs 4–5× slower
on its *second* run under ROCm. Upstream. Restarting ComfyUI clears it.

---

## 6. Applying fixes to kanto

First find out whether you can reach kanto's shell:

```powershell
ssh dylan@kanto.tail4f3755.ts.net "echo ok"
```

**If that works**, edit directly:

```
/home/dylan/photodump/.env               # CHECKPOINT, WAN_UNET, WAN_CLIP, WAN_VAE
/home/dylan/photodump/app/workflows/*.json
```

Then **`cd /home/dylan/photodump && docker compose up -d --build`**.
The code is baked into the image — a plain `restart` ships nothing.

Re-run `./tools/smoke.sh` afterwards (16 assertions, needs no GPU).

**If SSH does not work**, do not improvise a transport. Write your findings as
an exact diff — file, node id, current value, corrected value, and the
`object_info` evidence — and hand that back to Dylan. Being precise is worth
more than being clever here.

Note `.env` has no `WAN_*` lines yet; they fall back to defaults in
`app/config.py`. Add them when you set up video:

```
WAN_UNET=wan2.2_ti2v_5B_fp16.safetensors
WAN_CLIP=umt5_xxl_fp16.safetensors
WAN_VAE=wan2.2_vae.safetensors
```

---

## 7. Optional, after a still renders

**IP-Adapter** — install the `ComfyUI_IPAdapter_plus` node pack and its models.
Until then the UI greys that option out and preflight says
`missing nodes: IPAdapterUnifiedLoader`. That is correct behaviour, not a bug.

### Video — WAN 2.2 vs LTX-2.5

Two viable models. **Get a still rendering before attempting either.**

| | WAN 2.2 TI2V-5B | LTX-2.5 |
|---|---|---|
| Fits 16GB | yes, natively at fp16 | only via GGUF, tightly |
| Clip length | ~3–5s | **6–20s** |
| Audio | none | **synchronised, same pass** |
| Extra nodes | none | `ComfyUI-GGUF` |
| Risk | low | moderate |

**Start with WAN 5B.** It fits without quantisation, so it isolates "does video
work at all" from "does an aggressive quant fit". Once a clip comes out, LTX-2.5
is the upgrade worth making — longer clips and generated audio are both directly
useful for Instagram reels.

**LTX-2.5 on 16GB** needs community GGUF quants for *both* halves; the official
files total 34GB+. The known-working pairing is:

| file | approx |
|---|---|
| `LTX25-distilled-DiT-Q3_K_M.gguf` (transformer) | 10.6 GB |
| Gemma-4 text encoder, Q5_K_M GGUF | 9.5 GB |
| `ltx-2.5-video-vae-bf16.safetensors` | — |
| `ltx-2.5-audio-vae-bf16.safetensors` | — |

It fits only because ComfyUI frees the text encoder before sampling, and with
**tiled VAE decode** enabled. Q3_K_M is a hard quant — judge the output before
committing to it. Loaders are `UnetLoaderGGUF` and `CLIPLoaderGGUF` with
`type: ltxv`.

Note the GGUF route sidesteps §5 entirely, since nothing is fp8.

**Adding LTX to the app** is not just a new JSON: `app/comfy.py` has a
`_build_video()` that is specific to WAN's node layout. A second video model
needs a sibling builder plus `LTX_*` filename settings alongside the `WAN_*`
ones. Export the official template first, then write the builder to match it —
do not write the builder speculatively.

**WAN 2.2 TI2V-5B files**, ~15–20GB:

| file | goes in |
|---|---|
| `wan2.2_ti2v_5B_fp16.safetensors` | `models/unet/` |
| `umt5_xxl_fp16.safetensors` **or GGUF** | `models/text_encoders/` |
| `wan2.2_vae.safetensors` | `models/vae/` |

Start with 5B. The 14B needs dual high/low-noise models plus GGUF to fit 16GB.

---

## 8. Things that will mislead you

- **A `queued` job is not stuck.** Jobs wait deliberately while this PC sleeps.
  Only `failed` means something is wrong.
- **kanto's `smoke.sh` passing proves nothing about this machine.** It runs
  against a mock ComfyUI and never touches a GPU.
- **`ComfyOffline` vs `ComfyError` is load-bearing** in kanto's code. Unreachable
  node → requeue. Rejected graph → fail. Do not collapse them.
- **A job that failed 5 times is not lost.** Fix the cause and hit **requeue** in
  the Queue tab — attempts reset, prompt preserved.
- **`docker compose restart` ships no code changes.** Always `up -d --build`.
- **Do not deploy OpenCut** if editing comes up. Checked 2026-09-09: mid-rewrite,
  its `/editor` route body is literally "Coming soon". Use DaVinci Resolve here.

---

## 9. Report back

When you are done, say plainly:

1. Which of the five workflows now render for real, with an image to show
2. Every graph correction you made — file, node id, before, after
3. Anything preflight still reports blocked, and why
4. Whether video works, and roughly how long a 3s clip takes
5. Anything in this brief that turned out to be wrong

That last one matters. This brief was written by a session that could never
reach a GPU.
