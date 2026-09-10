# photodump — session summary, 2026-09-09

Written for you to review after a day away. Everything below is committed;
the app is running and clean.

**Live:** https://kanto.tail4f3755.ts.net:8452 · loopback `:8096`
**Tests:** `./tools/smoke.sh` — 16 assertions, 16/16, no GPU needed
**Commits:** 13 · working tree clean · library empty (test data cleared)

---

## You can use one thing today

**Build a reel from photos on your phone.** It needs no GPU at all — reels
assemble on kanto with ffmpeg. Upload images under **References**, switch to
the **Reel** task, set *shots from* to **my photos**, pick them in the gallery
in order, and build. Output is 1080×1920 with Ken Burns motion, cuts on a BPM
you set, and optional music.

Verified: four photos of mixed aspect ratios → a 9.6s 1080×1920 h264 reel with
the render node completely offline.

**Everything else needs the desktop.** Fuse, Extend and Animate all render on
the GPU, and nothing has ever run against a real ComfyUI.

---

## What got built

**Both Claude Design artboards are implemented.** Desktop and mobile, ported
from project `3c802983`. Mobile is a real information architecture — bottom tab
bar, node strip pinned on every screen, action bar above the nav — not a
reflow. Verified in headless Chromium: no console errors, zero horizontal
overflow at 390 / 768 / 1440.

**Preflight.** Under the render-node card. Once the desktop is awake it walks
all five workflow graphs against the node's `/object_info` and names, per
workflow, the missing node class or the model filename it wanted. Tested
against four broken configurations, including the one you will actually hit:
the placeholder checkpoint filename in `.env`.

**Requeue.** A job that burned through its 5-attempt ceiling against a broken
graph is no longer dead — fix the cause, hit requeue, it runs again with the
prompt intact.

**Reels from uploaded photos.** Shots are now `{src, id}`, so renders and
uploads mix freely.

**Instagram delivery encode.** New. Turns any clip or reel into a file
Instagram will accept: fit-and-pad to 1080×1920 (or 4:5 / 1:1), H.264 High,
controlled 6.5 Mbps, AAC, `+faststart`. Runs on kanto, so it works while the
desktop sleeps. This fixed two real defects — Animate was outputting `.webm`,
which **Instagram rejects**, and the reel encoder had no bitrate ceiling, which
makes Instagram's re-encoder treat it *worse*, not better.

**Video backend is now table-driven**, so adding LTX-2.5 is a table entry plus
an exported workflow rather than new code.

---

## Verified vs not — read this before trusting anything

**Verified:** everything in `smoke.sh`, the UI in real Chromium, reel assembly
including from uploads, the Instagram re-encode, requeue end to end, preflight
against four broken node configurations.

**Not verified — and this is the whole risk:**

> **No part of this has ever talked to a real ComfyUI.** Your desktop was
> offline for every minute of this work. No model has loaded; no real image has
> been generated. The five workflow graphs were written from documentation.
> Expect them to be wrong in places.

`wan_i2v.json` is the least certain of the five.

---

## Waiting on you

**1 — Stand up the desktop.** `SETUP-DESKTOP.md` is the ordered checklist.
`DESKTOP-SESSION.md` is a self-contained brief you can paste into a Claude
session **on the Windows machine** — it embeds the graphs and drives everything
through the tailnet API, so that session does not need the repo.

The single most useful thing in it: **do not debug my graphs.** ComfyUI ships
official WAN and LTX templates — open one, `Workflow → Export (API Format)`,
and you have a graph that is correct for the version you installed. Reshape
that onto kanto's node ids. Minutes instead of an evening.

**2 — Decide on LTX-2.5.** You asked; here is the short version.

- **Free for you.** LTX-2.x Community License, commercial use free under $10M
  revenue.
- **Fits your goal well.** Short Instagram clips sit at LTX's cheap end. It is a
  multi-minute series that would have strained the card, not this.
- **Two real advantages over WAN for Instagram specifically:** synchronised
  audio generated in the same pass, and **24–50fps against WAN's 16fps** — the
  delivery pass currently has to pad WAN's output up to 30, which cannot add
  smoothness that was never generated.
- **The catch:** it does not fit 16GB natively (official files total 34GB+).
  You would run `Q3_K_M` transformer + `Q5_K_M` Gemma encoder, fitting only
  because ComfyUI evicts the encoder before sampling. Aggressive quant, real
  quality cost, needs the `ComfyUI-GGUF` pack.
- **Speed, honestly extrapolated:** ~1–3 min per 4–6s clip at 540–720p. Nobody
  has published RDNA4 LTX-2.5 numbers. The headline "10s clip in 6.8s" figures
  are NVIDIA GB200.

**Recommendation: WAN 5B first.** It fits without quantisation, so it separates
"does video work on this card" from "does an aggressive quant fit". Then move to
LTX — for your use case it is the better model.

---

## Corrections I owe you

Two things I got wrong this session and fixed:

**OpenCut.** I recommended it for CapCut-style editing off the back of search
results. The actual repository is mid-rewrite — `main` is a scaffold whose
`/editor` route body is literally "Coming soon", and no branch has a
Dockerfile. Deploying it would have given you a placeholder page. Use DaVinci
Resolve on the desktop instead. README, HANDOFF and memory all say so now.

**Wake-on-LAN.** I first said it was impossible. Direct WoL from kanto is —
kanto is not on your LAN and magic packets are layer 2 — but that is not the
same as impossible, which is how I phrased it. You have since dropped it, so it
is out of scope; the paths were an always-on box on your LAN, a Tailscale-capable
router, or Windows scheduled RTC wake.

---

## Documents in this repo

| file | for |
|---|---|
| `SESSION-SUMMARY.md` | this |
| `SETUP-DESKTOP.md` | the Windows checklist, for you |
| `DESKTOP-SESSION.md` | paste into a Claude session on the desktop |
| `HANDOFF.md` | for anyone reviewing the code |
| `DESIGN-BRIEF.md` | the contract a UI redesign must not break |
| `design-prompt.md` | what was fed to Claude Design |
| `README.md` | how it works and why |
