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

### Still open

- Garbled fake lettering inherited from the reference's text regions. Much
  reduced by pass 2 but not gone; text/watermark negatives do not clear it.
- Backgrounds come out plainer than the reference's layered composition.
- Output caps around 680x850: **zero upscale models and zero LoRAs are
  installed.** That is also why portrait stills sit outside Instagram's 4:5
  limit and the delivery pass rejects them.

### Implementation

`second_pass` / `second_pass_denoise` in `app/main.py`; the chaining lives in
`worker._queue_second_pass`, which enqueues pass 2 with `src_image_id` set to
pass 1's image when pass 1 completes. The Create checkbox is `cr-stylematch`.
