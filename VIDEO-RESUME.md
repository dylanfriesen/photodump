# Resuming an interrupted video render

Pause stops a render and frees the card. Resume currently restarts that job
from step 0. For a still that is the right trade — a 30-step SDXL render is
about 30 seconds. For video it is not: job 27 (WAN 2.2 TI2V-5B, 704x1280)
took **787 seconds to reach step 21 of 30**, so a pause at 70% throws away
about thirteen minutes.

This is what it would take to do better, and the one thing that blocks it.
Everything below was verified against the live node on 2026-09-10, not read
from documentation.

---

## The mechanism, which works

ComfyUI can sample in chunks: `KSamplerAdvanced` takes `start_at_step` /
`end_at_step` and `return_with_leftover_noise`, so a 30-step render becomes
six chained samplers of five steps, each followed by a `SaveLatent`. Interrupt
during chunk four and chunks one through three are already on disk.

The round trip is confirmed working:

1. `SaveLatent` writes to ComfyUI's **output** directory.
2. `GET /view?filename=...&type=output` downloads it.
3. `POST /upload/image` with `type=input` puts it in the **input** directory.
4. `LoadLatent` enumerates input/ and the file appears in its combo.

Step 3 is not optional. `LoadLatent.latent` is a combo populated from input/,
and ComfyUI **validates combo values at submit time** — a resume graph naming
a file that is not in that enumeration is rejected with a 400 before anything
renders. `comfy.upload_latent()` exists for exactly this, and
`worker._checkpoint()` already harvests the furthest checkpoint from an
interrupted prompt's history and records it on the job.

So the plumbing is in place. What is missing is the chunked graph.

## The blocker: WAN's noise mask

`Wan22ImageToVideoLatent` returns **two** things in its latent dict
(`comfy_extras/nodes_wan.py`, ~line 1450):

```python
out_latent["samples"]    = latent      # the video latent
out_latent["noise_mask"] = mask        # zeroed over the start-image frames
```

The mask is what pins the opening frames to your reference image — the sampler
restores those positions every step instead of denoising them.

`SaveLatent` does not save it (`nodes.py`, ~line 553):

```python
output["latent_tensor"] = samples["samples"].contiguous()
output["latent_format_version_0"] = torch.tensor([])
```

Only `samples`. `noise_mask` is dropped on the floor.

Resume from a `LoadLatent` therefore hands the sampler a latent with no mask.
The start frames stop being pinned, get denoised as though they carried
`sigma[N]` worth of noise when they are already clean, and drift off the
reference. It would not error. It would just quietly stop looking like the
photograph you started from — the worst failure mode available.

**Stock ComfyUI cannot rebuild it.** `SetLatentNoiseMask` reshapes its input to
`[B, 1, H, W]`; WAN's mask is 5-D `[B, 1, T, h, w]` and the difference is the
temporal axis, which is the entire point. There is no stock node that carries a
mask across a save/load boundary.

## The two ways forward

**A. A small custom node on the desktop.** Roughly twenty lines in
`custom_nodes/`: take a loaded latent and a freshly built
`Wan22ImageToVideoLatent` output, copy `noise_mask` from the second onto the
first. The resume graph then rebuilds the mask from the same reference image it
started with, which is correct by construction.

Cost: photodump stops working against stock ComfyUI. The node has to survive
ComfyUI updates, and a reinstall that forgets it produces drifting video rather
than an error. `preflight.py` already validates node classes against the node,
so it can name the missing node loudly — that is the mitigation, and it should
land in the same change.

**B. Leave it.** Pause on video stays "stop and restart from zero". Honest,
already working, costs a re-render.

A is worth it if you pause long video often. B is worth it if pause is mostly
"give me the GPU back, I will rerun it tonight" — which the scheduler now
covers, since you can pause and set `not_before` for later.

## Unproven

Nothing here has run on the GPU. The round trip was tested with a dummy file
and `object_info`; the chunked graph has never been submitted, and the claim
that chunk boundaries are visually seamless is untested. Sampler state other
than the latent — anything a sampler carries between steps, which for ancestral
samplers includes its own noise schedule — may also not survive a chunk
boundary cleanly. Test with `uni_pc` (the WAN default) before trusting it.
