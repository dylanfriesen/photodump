# Recipes

Proven parameter sets. Each one records *why* the numbers are what they are, so
the next session tunes from evidence instead of re-deriving it.

---

## Retro cel style match (two-pass img2img)

**Use it when:** the output must match a reference image's *rendering
technique* — its painting style, lighting and line quality — rather than its
subject.

**In the UI:** Create → pick one reference → set *how much to change* to `0.45`
→ tick **style match**. Pass 2 is queued automatically when pass 1 lands.

**Over the API:**

```sh
curl -X POST localhost:8096/api/generate -H 'Content-Type: application/json' -d '{
  "prompt": "<subject tags>, retro artstyle, 1990s (style), airbrush, gradient shading, dramatic rim lighting, dark moody background",
  "negative_full": "(green hair:1.7), (<reference hair colour>:1.6), text, watermark, signature, username, bright colors, oversaturated, flat color, bad quality, worst quality, bad hands, bad anatomy",
  "workflow": "img2img",
  "ref_ids": [4],
  "denoise": 0.45,
  "steps": 40,
  "second_pass": true,
  "second_pass_denoise": 0.65,
  "count": 2
}'
```

### Why two passes

**Pass 1 (denoise 0.45)** reproduces the reference's rendering faithfully. The
value is not arbitrary — a sweep at 0.45 / 0.55 / 0.65 / 0.75 showed 0.45 nails
it, 0.55 is noticeably weaker, and by 0.65 the output has drifted back to the
checkpoint's own glossy modern look.

**Pass 2 (denoise 0.65, from pass 1's output)** fixes colour. Pass 1 inherits
the reference's *colours* along with its style: a platinum-blonde character
rendered from a dark-haired reference comes out dark-haired. Negative-prompting
the dark hair away at 0.45 produces **green** hair rather than blonde — the
constraint is *luminance in the source latent*, not prompt strength, and a dark
region cannot become light without a denoise high enough to destroy the style.
Pass 2 escapes that bind: the retro style is native to pass 1's output, so a
higher denoise corrects colour while leaving the style intact.

### What does not work, and why

- **Prompt tags alone.** `waiNSFWIllustrious_v14` is the only checkpoint
  installed and it renders glossy modern anime regardless of `flat color`,
  `limited palette`, `retro artstyle` or an added hires pass. Four rounds failed.
- **IP-Adapter.** It transfers *subject and palette*, not rendering technique.
  At weight 0.45 it pulled the reference's red background into the character's
  hair; at 0.85 with `strong style transfer` it pulled in unrelated hair colour
  and a striped texture. Use it to carry a character or a colour scheme — never
  to copy how something is painted.
- **A hires pass.** Adds resolution, not style.

### Known-good outputs

`data/out/64_1170046077_0.png`, `66_83980300_0.png`, `70_648109459_0.png`
(job 70 is the chained one-click path, end to end).

**Saved-record audit, 2026-09-11:** these are existing renders, not a new
experiment. Job parameters were read from the database without modifying it.

| Output in `data/out/` | Source | Denoise | Steps / CFG | Dimensions |
|---|---|---|---|---|
| `64_1170046077_0.png` | ref 7, manual colour pass | 0.55 | 40 / 5 | 680×856 |
| `66_83980300_0.png` | ref 7, manual colour pass | 0.65 | 40 / 5 | 680×856 |
| `69_1712170323_0.png` | ref 4, chained pass 1 | 0.45 | 40 / 5 | 680×856 |
| `70_648109459_0.png` | image 69, automatic pass 2 | 0.65 | 40 / 5 | 680×856 |

Do not label 64 as a 0.65 result. Jobs 69/70 used `second_pass: true` and
`second_pass_denoise: 0.65` on the first job; the second job records
`src_image_id: 69`. Future paired experiments must use this shipped chain.
Both chained jobs saved the same prompt and negative; the chain does not
automatically add stronger hair-colour tags for pass 2 (`worker._queue_second_pass`).

### Still open

- Garbled fake lettering inherited from the reference's text regions. Much
  reduced by pass 2 but not gone; text/watermark negatives do not clear it.
  Visual evidence: `data/out/64_1170046077_0.png` and
  `data/out/66_83980300_0.png` retain large left-edge lettering and a
  bottom-left logo. `data/out/70_648109459_0.png` still has a bottom-left
  R badge with tiny fake lettering. No new text-removal treatment was tested.
- Backgrounds remain simple: black plus red drapery in
  `data/out/64_1170046077_0.png` and `data/out/66_83980300_0.png`; broad dark
  teal and orange shapes in `data/out/70_648109459_0.png`. No new background
  treatment was tested.
- Resolution: all four outputs in the table are **680×856**, as verified
  from their PNG headers. **Zero image upscale models and zero LoRAs are
  installed.** Code inspection of `app/comfy.py:build` shows that `hires`
  selects a latent-upscale tail only for txt2img; img2img currently ignores
  that flag. An equivalent img2img tail remains **untested**, including
  whether it preserves cel shading. The earlier failure to create style
  with hires does not establish that hires destroys an already styled source.
  Scaling both dimensions equally also leaves the aspect ratio unchanged;
  680/856 is slightly below 4:5. Resolution and aspect correction are separate.
- Crop-top anatomy/garment boundary: uneven under-bust contours and hem
  openings remain visible in `data/out/64_1170046077_0.png`,
  `data/out/66_83980300_0.png`, and `data/out/70_648109459_0.png`. No anatomy
  correction was tested; retain this as a separate evaluation criterion.

**Experiment blocker, 2026-09-11:** this session could inspect existing images
and saved job metadata, but socket creation for both the local app and desktop
ComfyUI failed with `PermissionError: [Errno 1] Operation not permitted`.
Docker socket access was also denied. This is a session permission failure,
not evidence that ComfyUI is offline or a recipe failed. No new generations,
parameter sweeps, code changes, or rebuilds were performed. There are no new
output filenames supporting a fix for any of the four defects. Keep the
documented 0.45 → 0.65 / 40-step recipe pending at least two visually inspected
outputs per proposed change through `second_pass`; do not count this audit as
render verification or mark unrun approaches as ruled out.

### Built 2026-09-13 for the open items — NOT yet rendered

Built and tested against the mock only; not one of these has run on the RX
9070. Each is an experiment waiting for a GPU session, not a recipe. Both
upscale graphs were checked against the desktop's real `/object_info` (every
node class and input exists), which proves they will be *accepted*, not that
they look good.

| Open item | What exists now | What the GPU session has to settle |
|---|---|---|
| Lettering | `clean_regions` on img2img: boxes filled out of the reference on kanto before pass 1 (`imageops.fill_regions`). Create has a drag-to-draw pad with an exact preview. | Does a filled reference at 0.45 stop the fake text without leaving a visible smudge? The fill is smooth colour, not texture. |
| Hair colour | `second_pass_prompt_add` / `second_pass_negative_add`: tags only pass 2 gets. Create shows *pass 2 adds / avoids* when style match is on. | Does `platinum blonde hair` on pass 2 alone reach blonde from 69-style dark hair at 0.65? |
| Resolution | img2img `hires` tail, **on pass 2 only** when chained. `hires_method: pixel` (default: lanczos 1.5x, re-encode, denoise 0.35) or `latent` (bicubic latent 1.5x, denoise 0.45). Capped at 2.3MP. | Which route keeps the cel shading? Sweep pixel at 0.25 / 0.35 / 0.45 against latent at 0.45 / 0.55; score with `tools/compare.py` **and look**. |
| 4:5 | Stills deliver as a 1080x1350 q95 JPEG (lightbox → *Make Instagram JPEG*). 680x856 crops 6px; anything >4% off pads. | Nothing — this one runs on kanto and is verified. |

**Why pixel is the default route:** a bicubic latent upscale is blurry and
needs roughly 0.5 denoise to resolve. The style sweep above showed 0.55 already
weakening the style, so the latent route is expected to fight the thing it is
meant to preserve. A lanczos pixel upscale stays sharp and can be refined
gently. This is reasoning, not a result — the sweep may overturn it.

**Suggested sweep** (ref 4 with the name text, watermark and carousel buttons
boxed out; `count: 2` per cell): the recipe above plus `hires: true` and
`second_pass_prompt_add: "platinum blonde hair"`, varying only `hires_method`
and `hires_denoise`.

### Implementation

`second_pass` / `second_pass_denoise` in `app/main.py`; the chaining lives in
`worker._queue_second_pass`, which enqueues pass 2 with `src_image_id` set to
pass 1's image when pass 1 completes. The Create checkbox is `cr-stylematch`.
Kanto-side source prep (fill + upscale cap) is `worker._prep_img2img`; the
upscale graphs are `img2img_hires.json` (latent) and `img2img_hires_pixel.json`.
