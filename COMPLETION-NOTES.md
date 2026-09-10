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
