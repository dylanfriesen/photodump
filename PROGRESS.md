# photodump — progress log

Append-only record of what shipped and why. Newest first.

Every commit gets a one-line entry automatically via `.git/hooks/post-commit`
(so Codex's and Dylan's commits are logged too, not just Claude's). Anything
substantial also gets a short **why** written by hand — the automatic line says
*what*, the prose says *what it was for and what it cost*.

Read this before starting work to see where things stand. `COMPLETION-NOTES.md`
is the current handoff; `AT-THE-DESKTOP.md` is what needs doing in person.

---

## 2026-09-11

**LTX-2.5 runs, driven end to end from kanto**
It had never run. The `LTXV*` progress handling written earlier was written
ahead of the fact, not from observation - ComfyUI's history held zero LTX
graphs. I had inferred otherwise and should have checked.

The blocker was never a missing click. ComfyUI's bundled
"Image to Video (LTX-2.5)" blueprint loads the transformer with `UNETLoader`,
which only sees safetensors; the installed transformer is a Q4_K_M **GGUF**,
the right choice for 16GB. So that blueprint fails on load every time,
regardless of who launches it.

Built a single-pass graph by expanding the blueprint's 47-node subgraph against
the node's live `/object_info` - not from documentation - swapping in
`UnetLoaderGGUF` and `CLIPLoaderGGUF (type: ltxv)`, and dropping the second
upscale pass because `ltx-2.5-latent-spatial-upscaler-x2` is not installed.
Accepted on the first submit. 768x512, 97 frames, 24fps, with generated audio,
in 1971s.

Three bugs the wiring exposed, all found by testing rather than reading:
config still held **guessed** filenames (`LTX25-distilled-DiT-Q4_K_M.gguf` does
not exist, and the configured Gemma GGUF is a 148-byte failed download); the
builder hardcoded text nodes 6/7, which are WAN's - in LTX those are the
preprocessor and the empty latent, so prompts were written into the wrong nodes;
and `video_backend` never reached the params, so every request would silently
have used WAN. Text node ids are now per-backend, and preflight validates 7/7
including both video backends.

**Cost:** ~33 minutes per 4-second clip at 768x512, and about an hour at
544x960. Slower than WAN's 20.6 minutes, for audio and better motion. This is a
"set it going" tool on a 16GB card, not one to iterate with.

**Open:** the latent upscaler is not installed, so output is capped at
first-pass resolution.


**Benchmarked the render node, and fixed a 20-second dead wait it exposed**
First real performance measurements now that ComfyUI is reachable.

Pure sampler time at 832x1216, measured from ComfyUI's own execution
timestamps rather than wall clock:

| steps | seconds |
|---|---|
| 15 | 6.6 |
| 20 | 7.8 |
| 25 | 9.1 |
| 30 | 10.3 |
| 40 | 12.7 |
| 50 | 15.2 |

Linear: roughly **2.9s fixed + 0.25s per step**. Resolution barely matters —
square, portrait, landscape and story all land within 0.3s of each other at 30
steps, because every SDXL bucket is about one megapixel. First render after a
cold start costs ~27s while the checkpoint loads into VRAM.

**The fix:** end-to-end through the app took 30.7s for a single 10.3s render.
The worker's idle poll was 20s, so a freshly queued job sat untouched for
19-20s (measured three times: 18.9, 20.1, 19.5). Enqueueing now sets an
`asyncio.Event` the worker waits on, so pickup is immediate while the 20s
heartbeat still refreshes node status.

Pickup **19.5s -> 0.3s**. One image end-to-end **30.7s -> 13.6s**; four
**73.7s -> 52.3s**. Remaining overhead is the 2s completion-poll granularity
plus fetch, thumbnail and DB write — about 2.8s per image.

**Cost:** nothing wasted; the benchmark was the thing that found it. Worth
noting the bug was invisible without measuring, since every individual piece
behaved correctly.

**Also measured:** IP-Adapter roughly doubles a render (19-27s vs 10s), and one
WAN video clip took **1237s — 20.6 minutes**.

36 smoke assertions and 12 unit tests pass. Deployed.

## 2026-09-10

**Gallery masonry never recomputed on resize**
Reported as "the whole gallery does not scale". `#grid` is a 1px-row masonry:
each tile's height is a `grid-row-end: span N` in pixels, computed by
`sizeTile()` from `fig.clientWidth`. It ran **once**, on image load, and there
was no resize handling anywhere - so any change in column width left every tile
claiming its old height. The images rescale with `width:100%`; the rows they
occupy do not. Tiles overlap or float in gaps.

Worst at the 900px breakpoint, where the grid goes three columns to two *and*
the sidebar collapses, so the column width changes twice over in one step.

Fixed by remembering each tile's aspect ratio on the element and recomputing
the span from it, driven by a `ResizeObserver` on `#grid`. Storing the ratio
means a relayout needs no image reload; observing the grid also catches the
gallery tab becoming visible, when tiles that previously measured 0 wide
finally have a width. The write is skipped when the span is unchanged, because
writing one can move the scrollbar, which resizes the grid, which would call
straight back in.

**Worth recording:** this was blamed on the pause/stop work in the same session
and was not - that commit touched job rows and the status line, and the running
container was still serving pre-change assets. Checking what the server
actually served settled it in one command, before any code was read.

Also guarded `$('queue-pause').onclick` from the same session: an unguarded
handler on a missing element throws at load and kills every handler defined
after it, which presents as most of the UI being dead rather than one broken
button.


**Pause, stop and scheduling; video resume investigated and not shipped**
Three controls the queue never had. Stop interrupts the node and marks the job
terminal. Pause interrupts *and* calls `/free` - that second half is the point,
since interrupting stops sampling but leaves a 10-12GB checkpoint resident in
VRAM, which is the whole reason to pause. Queue pause stops dispatch without
touching the running job. Scheduling is a `not_before` compared in SQL.

Two traps, both of which would have shipped silently:

1. *Pause must not spend an attempt.* `_claim` does `attempts+1` and
   `MAX_ATTEMPTS` is 5, so routing pause through `_release` fails a job
   permanently on its fifth pause with "gave up after 5 attempts" - the exact
   message that means "this graph is broken". `_pause` decrements instead, and
   a smoke assertion now pins it.
2. *`paused` had to be a real status, not a flag on `running`*, because
   `recover()` requeues everything still marked running at startup. As a flag
   it would have survived testing and un-paused every held job on the next
   deploy.

Pause and stop are requests written to a `control` column, not statuses written
by the API: the worker owns `status` for the job it is rendering.

**Video resume: verified the mechanism, found a blocker, did not ship it.**
Job 27 took 787s to reach step 21 of 30, so restarting a paused video from zero
is expensive and resume looked worth building. The save/load round trip does
work, and it is not obvious: `SaveLatent` writes to `output/`, `LoadLatent`
enumerates `input/`, and ComfyUI *validates that enumeration at submit time*,
so the file must be round-tripped through `/upload/image` or the graph is
rejected with a 400 before rendering. `comfy.upload_latent()` and
`worker._checkpoint()` implement that half and are in the tree.

What stops it: `Wan22ImageToVideoLatent` returns `noise_mask` alongside
`samples`, and `SaveLatent` persists only `samples`. That mask pins the opening
frames to the reference image. Resuming without it denoises already-clean
frames as though they carried `sigma[N]` of noise, and the clip drifts off the
source photograph - no error, just wrong output. `SetLatentNoiseMask` cannot
rebuild it; it reshapes to 4-D and WAN's mask is 5-D over the temporal axis.

**Cost:** roughly half the session went into a feature that did not ship. It was
still the right order - the alternative was shipping a resume that silently
degrades video, which is worse than not having resume. Written up in
`VIDEO-RESUME.md` with both source files quoted, and it needs a decision: a
small custom node on the desktop, or leave video resume out.

Everything here was verified against the live ComfyUI (967 node classes) rather
than from documentation, per the standing rule about exported graphs.

36 smoke assertions, 12 unit tests. Not deployed.


**SSH to the render node works; the blocker was a username**
Three sessions had built kanto's half of remote access and stopped at the
Windows step. It finally ran, and then failed at key auth with
`Permission denied (publickey)` — which `~/desktop-ssh/README.md` documents as
the `administrators_authorized_keys` ACL trap.

It was not that. The ACLs were correct. **The Windows account is `drfxb`, not
`dylan`**, and `setup-kanto.sh` hardcoded the wrong name into `~/.ssh/config`.
The two failures are indistinguishable from kanto, so the documented gotcha
actively misled the diagnosis. Fixed the default and wrote the username into
the README as a second named gotcha; re-running the script would otherwise
reintroduce it on any fresh machine.

**Cost:** most of the Windows-side debugging was aimed at the wrong layer,
because a plausible documented explanation fit the symptom. Checking `whoami`
against `~/.ssh/config` would have cost ten seconds.

Also narrowed the firewall rule from `100.64.0.0/10` to kanto's tailnet IP.
The whole-CGNAT scope the enable script writes adds nothing: the tailnet ACL
is default allow-all and carries shared nodes belonging to another account, so
"tailnet-only" was never the boundary it sounded like. Verified from kanto's
public interface that ports 22/3389/8188 are unreachable at the desktop's
public IP.

**Corrected an assumption I nearly acted on:** ComfyUI binds the tailnet IP
rather than `0.0.0.0`, which I flagged as a boot-order race. It is not —
`start-render-node.ps1` waits five minutes for the address and falls back to
loopback. The bind is deliberate and more restrictive. `AT-THE-DESKTOP.md` now
says not to change it. ComfyUI also already autostarts at logon, which that doc
still listed as a manual step.

Still open: the LTX workflow export, which needs the ComfyUI GUI and so cannot
be done over SSH. Queue drained to zero, 26 renders on disk. No GPU validation
of the Create work yet.


**Second Codex review: four more findings, all fixed**
Two were bugs I introduced in the *previous* fix commit, which is the useful
lesson — fixing six things at once created two new ones.

1. *`syncCreate()` derived everything before normalising the selection.* It read
   the mode at the top and only reset an invalid `img2img` choice twenty lines
   later, so adding a second reference to a "keep composition" selection left
   denoise on screen, skipped the capability guard, and still submitted
   `ipadapter_multi` with `ip_weight`. Display and payload disagreed. Normalise
   first, then derive.
2. *Changing any control released the in-flight request lock.* `syncCreate()`
   set `btn-create.disabled` from capability alone, clobbering the busy flag —
   so nudging the count during a pending request re-enabled the button and let
   you submit twice. Busy and eligibility are now separate, combined in one
   place, with the submit handler re-checking both and clearing state in
   `finally`.
3. *A reference deleted between queueing and rendering silently shrank the
   blend* — or dropped to txt2img if all were gone, with nothing in the record
   saying so. The worker now fails the job naming the missing ids.
4. *Preflight's txt2img entry was validating an img2img graph*, because it was
   handed a dummy reference and auto-resolution picked img2img. Pre-dated the
   multi-reference work. It now passes none for txt2img and two for
   ipadapter_multi.

Also closed two limitations Codex noted short of defects: the API now rejects
`img2img` with several references (the UI blocked it, the endpoint did not), and
the post-commit hook is versioned at `tools/hooks/` with `install-hooks.sh`,
since `.git/hooks` never survives a clone.

**Cost:** the first fix pass was verified only against the states I thought to
try. Codex found the transition *between* states — one reference to two with a
stale selection — which is exactly where I had not looked.

Closed from the first review: all six. 24 smoke assertions, 12 unit tests,
deployed to live with data intact. Still no GPU validation.


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
- `20ed9fe` 2026-09-10 12:31 (dylan) — Fix six Create issues from Codex's review; deploy
- `1f36098` 2026-09-10 12:51 (dylan) — Fix four findings from Codex's follow-up review
- `f93b7ef` 2026-09-10 17:14 (dylan) — Record the SSH fix; retire the finished items in AT-THE-DESKTOP
- `ad0ec5e` 2026-09-10 17:59 (dylan) — Add pause, stop and scheduling to the queue
- `6148509` 2026-09-10 18:33 (dylan) — Recompute gallery tile heights when the grid resizes
- `0f4f12a` 2026-09-11 08:30 (dylan) — Wake the worker on enqueue instead of waiting out the idle poll
