# photodump — progress log

Append-only record of what shipped and why. Newest first.

Every commit gets a one-line entry automatically via `.git/hooks/post-commit`
(so Codex's and Dylan's commits are logged too, not just Claude's). Anything
substantial also gets a short **why** written by hand — the automatic line says
*what*, the prose says *what it was for and what it cost*.

Read this before starting work to see where things stand. `COMPLETION-NOTES.md`
is the current handoff; `AT-THE-DESKTOP.md` is what needs doing in person.

---

## 2026-09-10

**Fixed six issues Codex's review found in Create, and deployed it**
All six were real; I verified each before changing anything rather than
fixing on report alone.

1. *Reference strength was a no-op with one reference.* Auto resolves a single
   reference to img2img, whose strength is `denoise` — but the control always
   sent `ip_weight`, which that graph never reads. The UI now resolves the
   workflow client-side (`resolvedCreateMode()`, mirroring `comfy.build`) and
   shows the control that actually applies.
2. *Picking a Create reference retargeted the other tasks.* `refreshRefs()`
   rebuilds the Fuse/Extend/Reel selects, discarding their values, so choosing
   a Create reference could silently point Extend at a different photograph.
   Selections are now preserved across a rebuild, and picking repaints only the
   Create picker.
3. *Create drafts were lost on reload* — the fields were missing from `REMEMBER`,
   which also only handled strings and only listened for `input`. Checkboxes and
   selects now persist too.
4. *Create bypassed the capability check.* Only Fuse's IP-Adapter option was
   disabled when the node lacks the pack; Create could still submit that
   workflow. `NODE_CAPS` now gates both, with a warning and a blocked Generate.
5. *"Keep composition" silently dropped extra references* — img2img loads only
   the first. That option is now disabled above one reference.
6. *The count badge disagreed with the payload* — the shared stepper set
   `input.value` without dispatching `input`, so Create's badge never updated.
   The stepper now fires the event, which fixes it for any future stepper too.

Also replaced the picker's innerHTML rebuild with in-place updates. Rebuilding
on every click detached the tile mid-interaction and discarded focus and scroll.

**Cost:** my own test was wrong twice — it clicked stale DOM nodes after a
rebuild, and used `await` inside `Runtime.evaluate`, which silently returns
undefined. Both produced false failures I chased before checking the harness.

**Deployed to the live instance** (`up -d --build`); 6 jobs and 3 refs intact.
24 smoke assertions and 12 unit tests pass.

**Still open:** ComfyUI unreachable, jobs waiting, LTX submission unimplemented
pending the API-format export. No GPU validation of any of this.

## 2026-09-10

**`3942f72` Create task — prompt directly, with any number of references**
The app only offered Fuse/Extend/Reel, so the two-subject blend was the *only*
route to an image. Create is now first and default. Multiple references are
batched via core `ImageBatch` into one IP-Adapter, because img2img conditions on
a single latent and cannot take more. Caught a self-inflicted bug: `e.currentTarget`
is null after an `await`, so the button never re-enabled and the job silently
failed to queue. Verified on an isolated instance, port 8097.

**`febbcee`, `ea3ab28` Corrected stale status claims**
`HANDOFF.md`, `SESSION-SUMMARY.md` and `DESKTOP-SESSION.md` all asserted nothing
had ever run against a real ComfyUI. True when written, false now — stills have
rendered on the RX 9070. Flagged by Codex's handoff.

**`e3be234` AT-THE-DESKTOP.md** — three ordered in-person actions: start ComfyUI
with `--listen`, export the working LTX workflow in API format, run the SSH
bootstrap.

**`d6df696` Connection failures distinguished from a sleeping desktop**
The offline copy assumed "asleep", which is wrong when the machine is awake and
ComfyUI simply is not listening — exactly the state that cost most of a day.

**`d8f0e7e` Progress UI and persistent render email** *(Codex)*
Live sampler steps, stage, ETA, LTX's two sampling passes sharing one bar,
restart recovery that reattaches rather than resubmitting, and the mail worker.
SMTP credentials were configured and **verified against Gmail on 2026-09-10**.

## 2026-09-09

**`2627ff0` Instagram delivery encode; video backend made table-driven**
Animate was emitting `.webm`, which Instagram rejects outright, and the reel
encoder had no bitrate ceiling — which makes IG's re-encoder treat it *worse*.
Delivery now produces 1080-wide H.264 at a controlled 6.5 Mbps with faststart.

**`09dbfd1` Reels can use uploaded photos** — made the one GPU-free feature
usable before the GPU existed.

**`18c680f`, `e30eb42` Claude Design artboards implemented** — desktop and
mobile, as a responsive restructure of one DOM so all ids stay single-instance.

**`59ca57e` Preflight, job requeue, desktop setup checklist**
Preflight validates every workflow graph against the node's `/object_info` and
names what is missing, because the graphs were documentation-derived and
expected to be wrong.

**`a608982` Corrected the OpenCut recommendation** — I had recommended it from
search results; the actual repo is mid-rewrite with a "Coming soon" editor stub.

**`1c6137f` Renamed anime-forge to photodump**, added the ffmpeg reel assembler.
`zoompan`'s `d` is output frames per *input* frame — with a looped input that
rendered ~3100 frames for a 1.9s shot. Fixed to `d=1` driven by `on`.

**`f0deb48` Outpainting, WAN 2.2 video, requeue ceiling**
A sleeping PC and a ComfyUI crashing on one graph look identical from kanto, so
requeues are capped at 5 attempts rather than looping forever.

**`bb671ad` Initial: queue-backed studio** — the queue is the architecture, not a
convenience. Wake-on-LAN cannot cross the tailnet from a public-IP host.

---

## Log
- `eff568c` 2026-09-10 12:13 (dylan) — Add PROGRESS.md and a post-commit hook that maintains it
- `4b4a015` 2026-09-10 12:14 (dylan) — Add a working agreement both agents read
