# Photodump continuation — September 15, 2026

**Start here.** Nothing below needed the GPU. It is all live on kanto.

- **Next desktop wake:** kanto installs the 03:00 night-drain task over SSH.
  Check `~/.photodump-night-drain-install.log` for the `powercfg /a` output and
  the self-test lines.
- **Next render session**, which should now be the first night after that:
  sweep `upscale-hair-lettering-1` renders. Open `/sweeps.html`, look at the
  pass-2 column per cell, and write the verdict into RECIPES.md. It decides
  pixel vs latent upscaling, whether pass-2-only hair tags reach blonde, and
  whether the lettering fill leaves a smear.
- **New and verified without a GPU:** backups (first snapshot taken), reel
  hook/cover/offset/carousel, the crossfade music-fade fix, the results page.
  Tests: 70 unit, 43 smoke, 76 browser (`tools/ui_check.sh`).
- **Header:** the design's "settings" slot now links to sweeps. There is no
  settings screen, so nothing was lost.

---

# Photodump continuation — September 14, 2026

## September design implemented

Finished Claude's interrupted implementation of **Photodump v2 Artboards.dc.html**
from [Claude Design project 3c802983](https://claude.ai/design/p/3c802983-07b0-421a-92cf-decaef471359?file=Photodump+v2+Artboards.dc.html).
Used the exact artboard and `support.js` already imported through Claude's
DesignSync MCP on September 14. Cached source:
`/tmp/claude-1000/-home-dylan/33d98f45-60fd-4405-9502-99001aa206ff/scratchpad/v2/`.
`support.js` is the design canvas runtime; the application keeps its existing
vanilla JS runtime and API rather than shipping the canvas editor.

- Create now has the reference plan card, numbered selection, two-pass panel,
  cleanup preview states, preview card, toasts, and a shared visible sampler.
- Node progress distinguishes measured, estimated, and unknown values; mobile
  keeps the node strip across screens. Capability controls recover on reconnect.
- Queue has hold/schedule, pause/stop/resume, error copy, and paired style-match
  rows. Pairing requires the same batch and an img2img child, so a later export
  or animation cannot masquerade as the second pass.
- Detail has paging position, touch swipes, keyboard navigation, grouped actions,
  Instagram format cards, six animation canvases and wired WAN/LTX selection.
  Create reference-based requests and Fuse recipes can restore saved settings;
  derived image-only jobs without a reusable reference recipe keep reuse hidden.
- Added keyboard control to reference/model/canvas choices and modal focus
  containment. Sampler text remains 16px on mobile to avoid input zoom.
- `/api/jobs` and `/api/images` now include the required lineage/settings fields
  and reject invalid or excessive limits before database access.

### Validation

- Existing unit suite: **46 passed**; new `tools.test_design_api`: **4 passed**.
- `tools/smoke.sh`: **43 passed, 0 failed**, from a separate repo copy under
  `/tmp/photodump-v2-validation` with its own data directory and mock node.
- Headless Chromium: **47 browser assertions**, plus **6 cleanup/swipe/keyboard
  checks**, no browser exceptions. Covered real API submissions to an isolated
  instance, local-time scheduling, model payloads, reuse, reference modes,
  progress, and 390/768/1440px layouts. Synthetic images only.
- Logs/screenshots: `/tmp/photodump-v2-*`; API regression tests are committed in
  `tools/test_design_api.py`. Run them with the same Docker mounts documented
  in AGENTS.md.

The live image was rebuilt. App networking remains loopback-only on 8096 with
`default` and `web` networks. Added the missing policy-required internal Caddy
vhost at `http://photodump.internal:8096` in `/srv/gooner/caddy/Caddyfile`, validated
and reloaded it. This does not publish a port or change Tailscale Serve routes.

No new GPU quality claims: desktop ComfyUI was unavailable. The unverified
image-quality and LTX render work in RECIPES.md / AT-THE-DESKTOP.md remains open.

---

# Photodump continuation — September 11, 2026

**Start here: `RECIPES.md`.** Image quality was the open problem for three
sessions and it is now solved — matching a reference's art style is a **two-pass
img2img chain** (denoise 0.45, then 0.65 from pass 1's own output), shipped as a
*style match* checkbox in Create. `RECIPES.md` has the exact parameters, the
sweep results behind each number, and the approaches already ruled out
(prompt tags, IP-Adapter, a hires pass) so they are not retried.

Open on images: garbled lettering inherited from reference text regions, plain
backgrounds, and a ~680x850 cap because **zero upscale models and zero LoRAs are
installed**. Video is parked at Dylan's request; the LTX second pass committed
in 71bb641 is **unverified** — its one test render finished implausibly fast.

Codex (`gpt-6-astra`) was mid-run on the remaining image defects when this
session ended; check `PROGRESS.md` and `RECIPES.md` for anything it landed.

---

# Photodump continuation — September 9, 2026

Claude stopped at its usage limit after implementing the first progress and
usability pass. The older session summary predates the working desktop node.

Completed refinements:

- Node and queue progress show live sampler steps, stage, elapsed time, and an
  estimated ETA. Unknown percentages stay indeterminate. LTX's two sampling
  passes share the bar instead of resetting it between passes.
- Restart recovery attaches to the existing ComfyUI graph and original
  telemetry client. It does not rebuild or resubmit an active LTX prompt using
  the old WAN job metadata. The node's original elapsed time is retained.
- Long renders remain attached while ComfyUI acknowledges the prompt; the old
  30-minute timeout no longer fails an active GPU render.
- Local Instagram encoding reports ffmpeg progress; finished output clears
  the progress display before the WebSocket shutdown handshake.
- Queue toasts, saved form values, lightbox navigation, recipe reuse, caption
  provenance, and an inline Instagram format picker complete Claude's UI pass.
- Gallery polling preserves loaded media while refreshing its captions and
  metadata. Network failures retry status polling and show action errors.

The persistent email worker watches completed jobs and emails every saved
render to the configured `MAIL_TO` address. Configuration, retries, attachment
limits, and delivery records are documented in [MAIL.md](MAIL.md).

Email sending requires `SMTP_USER` and `SMTP_PASS` in the private,
gitignored `data/mail/.env`. Completed renders remain queued until configured.

Validation tools:

```sh
python -m unittest tools.test_mailer tools.test_progress -v
./tools/smoke.sh
./tools/smoke.sh --progress-only
```

The smoke project uses a separate database and port 8097. Never remove the live
database while testing. Browser validation covers progress appearing after an
unknown initial value, gallery media preservation, form persistence, lightbox
navigation, network failures, and 390/768/1440-pixel layouts. A real ffmpeg
delivery encode also verified 1080×1920 at 30 fps with progress callbacks.

Deployment preserves the loopback-only app publish and existing tailnet URL.
The app joins `default` and external `web` with a stable alias. The mail worker
joins only `default` and publishes no ports. The Caddy Photodump vhost listens
on an unpublished Docker-internal HTTP port, so it adds no public access.
