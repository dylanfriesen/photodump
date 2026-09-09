# Setting up the render node (desktop-gqtd71t)

Everything here runs **on the Windows desktop**. kanto needs no changes except
the two `.env` lines called out in step 5.

Work top to bottom. After step 5 the app tells you what is still missing —
you do not have to guess.

---

## 1. Install ComfyUI

Install **ComfyUI Desktop for Windows**, v0.7.0 or later.

It ships with **AMD ROCm 7.1.1 prepackaged**, and your RX 9070 (gfx1201 /
Navi 48) is on AMD's supported list. You do **not** need ZLUDA, DirectML, WSL,
or a Linux dual-boot — ignore any guide that tells you otherwise; those
predate official support.

## 2. Make it reachable from kanto

ComfyUI binds to localhost by default, which the tailnet cannot reach. Launch it
with:

```
--listen 0.0.0.0 --port 8188
```

In ComfyUI Desktop this goes in Settings → Server → extra launch arguments.

Confirm from kanto:

```sh
curl -s http://100.109.223.93:8188/system_stats | head -c 200
```

Windows Firewall will likely prompt on first launch — allow it on private
networks. If the curl above times out, that prompt was probably dismissed.

## 3. Get a checkpoint

Download an **Illustrious-XL** or **NoobAI-XL** family SDXL checkpoint
(~6.5GB) into:

```
ComfyUI/models/checkpoints/
```

These are trained on danbooru tags, so they already know Gojo, Sukuna,
Gardevoir and friends by name. That is why the Fuse task is tag-blending
rather than fine-tuning — do not go looking for LoRAs to solve character
identity.

**Do not use an fp8 checkpoint.** See the warning in step 6.

## 4. Start Ollama (for captions)

Captions run on this same box, deliberately, so they share the render node's
availability rather than adding a second thing that can be down.

```
ollama serve
ollama pull qwen2.5:14b-instruct
```

Ollama must also listen beyond localhost — set `OLLAMA_HOST=0.0.0.0:11434`
in the environment before `ollama serve`.

## 5. Point photodump at the real filenames

On **kanto**, edit `/home/dylan/photodump/.env`:

```
CHECKPOINT=<exact filename from step 3, including .safetensors>
```

Then:

```sh
cd /home/dylan/photodump && docker compose up -d
```

`CHECKPOINT` must match the file on disk **character for character**. This is
the single most common day-one failure, and preflight catches it explicitly.

## 6. Read the preflight panel

Open https://kanto.tail4f3755.ts.net:8452 with the desktop awake. Under the
render-node card there is now a preflight strip:

- **teal, "5/5 workflows ready"** — you are done
- **amber, "N of 5 workflows blocked"** — click it. Each blocked workflow names
  either the missing ComfyUI node type or the model filename it wanted and
  could not find

Expect Fuse and Extend to pass first. IP-Adapter and Animate need the extra
installs below.

---

## Optional: IP-Adapter (the "borrow the look" workflow)

Install the **`ComfyUI_IPAdapter_plus`** custom-node pack plus its models. Until
then the UI greys that option out and preflight reports
`missing nodes: IPAdapterUnifiedLoader`.

## Optional: video (the Animate task)

Download the **WAN 2.2 TI2V-5B** set (~15–20GB total):

| file | goes in |
|---|---|
| `wan2.2_ti2v_5B_fp16.safetensors` | `models/unet/` |
| `umt5_xxl_fp16.safetensors` **or a GGUF** | `models/text_encoders/` |
| `wan2.2_vae.safetensors` | `models/vae/` |

Then set `WAN_UNET` / `WAN_CLIP` / `WAN_VAE` in `.env` to match.

> ### The fp8 trap — read this before downloading
>
> **FP8 (e4m3fn) is broken on RDNA4 under Windows.** It is documented as
> supported, but `torch.mm` / `torch.mul` raise `NotImplementedError` in
> practice.
>
> Nearly every published WAN workflow uses
> `umt5_xxl_fp8_e4m3fn_scaled.safetensors` as its text encoder. **That exact
> file will fail on your card.** Take the fp16 or a GGUF quant instead.
>
> Same rule for checkpoints: fp16 or GGUF, never fp8.

Also expect **ComfyUI issue #12672**: WAN 2.2 image-to-video runs 4–5× slower on
its *second* run under ROCm. Upstream bug, not ours. Restarting ComfyUI clears it.

The 14B WAN variant needs dual high/low-noise models and GGUF quantisation to
fit 16GB — start with 5B.

---

## When something fails anyway

A failed job in the Queue tab shows the node's own error, with **copy error**
and **requeue** beneath it. Fix the cause, hit requeue — the job runs again with
its attempt counter reset, so you never have to retype a prompt.

Jobs that merely sat unrendered because the desktop was asleep are **not**
failures; they stay `queued` and drain on their own when you wake the PC.
