Update the **photodump** design (Claude Design project `3c802983`, artboards
`PhotodumpDesktop.dc.html` and `PhotodumpMobile.dc.html`). Both artboards were
built into the real app on 2026-09-09. Since then about a dozen features have
shipped with UI I added myself, reusing your components as best I could. Some
fit well, some are bolted on. I need them designed properly, **in the existing
visual language** — lime `#d4f24a` / teal `#4fd6c0` / violet `#9b8cfa` on
near-black, Geist + Geist Mono, 3px radii, the render-node card as the anchor.
This is not a restyle. Keep everything that already works.

The context from the original brief still holds: one person, making anime art
for Instagram. Phone for queueing ideas, desktop for reviewing. The GPU is in a
desktop PC that's usually asleep, so **queued work is the normal state, not an
error.**

Below: what's new, how it behaves now, where it's weak, and the artboards I
need back. The appendix lists the element ids the code reads. Please keep them
so I can port the result straight back in.

---

## 1. Create: a new first tab, and now the main one

The studio task row was **Fuse · Extend · Reel**. It's now
**Create · Fuse · Extend · Reel**, and **Create is the default**. Fuse, the
two-franchise blend, used to be the only way to make an image. Create is a
free-form prompt, and most of my work happens here now.

### The simple case (no references)

- **Prompt**: a big textarea, e.g. *"a lone sorcerer on a rain-slick Shibuya
  crossing at night, neon reflections, cursed energy crackling"*
- **Add quality tags**: a checkbox, on by default
- **Avoid**: a text field
- **Canvas** segmented control (4:5 / 1:1 / 9:16 / 3:2) + **Count** stepper 1–8
- **Upscale 1.5× in a final refine pass**: a checkbox
- **Preview** (secondary) and **Generate ×4** (primary)

### Reference images: a numbered multi-picker

A 4-column grid of square thumbnails (3 columns on mobile), one per uploaded
reference. Tapping one selects it and gives it a numbered badge, in order, like
reel selection. Label at the top right: `2 selected`. Hint under the grid:
*"Click to add, in order. Several can be combined — they are blended as one
style reference."* Empty state: *"No references yet — upload some under
References."*

**The controls below the picker change with the number of references.** This
is the hardest part to design, because each reference count uses a different
graph behind the scenes and each graph reads different knobs:

| References | What happens | Controls shown |
|---|---|---|
| 0 | plain text-to-image | nothing extra; upscale checkbox shown |
| 1 | **keep composition** (img2img) by default | *how much to change* (denoise 0.1–1), *style match* checkbox, *how to use* dropdown, **clean-up pad** (below), upscale checkbox |
| 1, set to style only | IP-Adapter | *reference strength* (0.1–1.5), *how to use*; **no** upscale, **no** style match |
| 2+ | **style only**, always. "Keep composition" is disabled and relabelled *"keep composition — one reference only"* | *reference strength*, *how to use* |

Today these just show and hide in place, and the panel jumps around. I want the
mode to be **legible**. One glance should tell me "this will copy the layout of
ref #1" or "this will borrow the look of refs #1–3". Right now you have to work
it out from which inputs happen to be visible.

**Capability warning:** if the render node doesn't have the IP-Adapter pack,
the style-only option is disabled, the hint turns amber (*"The node has no
IP-Adapter pack installed — this would fail."*), and Generate is blocked.

### Style match: a two-pass render

This is the biggest quality improvement I've found. With one reference in keep
composition mode, the **style match** checkbox (*"copy the reference's
rendering, fix colour in a 2nd pass"*) queues **two chained jobs**. Pass 1 copies
the reference's art style at low denoise but also picks up its colours (a
blonde character comes out with the reference's dark hair). When pass 1
finishes, pass 2 re-renders from pass 1's own output at a higher denoise to fix
the colour.

When it's on, two more fields appear:
- **Pass 2 adds**, e.g. `platinum blonde hair`
- **Pass 2 avoids**, e.g. `dark hair, green hair`

These are tags that only pass 2 may use. Design ask: show that this is a
**two-step pipeline**, with pass 2's fields clearly belonging to step 2, not
just two more text boxes.

### Clean up the reference: a drawing pad

My references are often Instagram **screenshots**. img2img copies everything in
them, so name text, watermarks and carousel arrows come out as garbled fake
lettering. With one reference in keep composition mode, a pad shows the
reference image and I **drag rectangles** over the text. Those regions get
filled in from the surrounding pixels before rendering.

- Crosshair cursor; drag to draw (dashed while drawing, then a lime outline
  with a faint lime fill)
- Hovering a box turns it red; clicking removes it
- Count top-right: `3 boxes`
- **Preview fill** swaps the image for the actual filled result and hides the
  boxes; the button becomes **Back to boxes**. **Clear boxes** removes them all.
- Hint: *"Drag over lettering, logos and app buttons — img2img copies them as
  fake text. Click a box to remove it. Keep boxes off the character."*
- Must work with a finger on a phone. One finger draws; the page must not
  scroll while drawing.

Design it as a small tool, not a form field: an editing surface with its own
mode, box state and preview toggle.

### Preview

Create's **Preview** fills a plain `<pre>` (`#cr-preview`) with the prompt and a
plain-English line about how the references will be used:

```
+ masterpiece, best quality, amazing quality, very aesthetic, absurdres, a lone sorcerer on a rain-slick Shibuya crossing …

- (defaults)

1 reference(s) via img2img at denoise 0.45 + a 2nd colour-correcting pass
  pass 2 adds: platinum blonde hair
  pass 2 avoids: dark hair, green hair
  2 region(s) filled out of the reference first

upscaled 1.5× on the final pass in a refine pass
```

Fuse's tag preview already has your styled treatment (head row, count, copy
button). Create's doesn't. Bring it in line, and split it into "prompt" and
"plan".

### Two current gaps for you to solve

- **Create has no sampler controls of its own.** It quietly reads steps, CFG
  and checkpoint from inputs inside **Fuse's** collapsed *Reference image &
  sampler* section. The steps/CFG/checkpoint block should become shared studio
  settings, or Create should get its own. Either way there must stay **one**
  `#steps`, `#cfg` and `#checkpoint` in the DOM (see the appendix).
- **Create still uses `alert()`** for "Write a prompt first" and queue
  failures, and shows no confirmation when a job queues. Every other task uses
  toasts (§4). Create should too.

---

## 2. Live render progress in the node card

The render-node card is no longer just a state indicator. When a job is running
it shows **real progress**:

```
● rendering job #71 · 2 queued · 1 scheduled · 1 paused     ▮▮▮ 2
43%   step 13/30 · sampling pass 1/2              ~1m 12s left
━━━━━━━━━━━━━━━━━━━━░░░░░░░░░░░░░░░░░░░░░░░░░░░░░   ← the node-rule becomes a real bar
```

- **Percent** (lime, mono), **stage** (`step 13/30 · sampling`,
  `sampling pass 2/2`, `decoding video`, `encoding for Instagram`,
  `fetching result`, `reconnected; waiting for the next progress update`),
  **ETA** (`~1m 12s left`), or `4m 03s elapsed` when there's no ETA
- **Three honesty levels, and they must look different:**
  1. **measured**: the node reports real step counts. Solid lime bar with
     glow, lime percent.
  2. **estimated**: nothing reported yet; the number comes from how long this
     kind of job usually takes. Dimmer bar, grey percent, `(estimate)` after
     the stage.
  3. **unknown**: in flight but no number is honest. Percent shows `—`,
     stage + elapsed still shown, and the old indeterminate sweep animation
     stays on the rule.
- Renders range from **10 seconds** (a still) to **an hour** (an LTX video
  clip at 544×960). The design has to work for both. A bar that crawls for an
  hour must not look stuck.
- The same bar appears as a thin inline bar **on the running row in the
  Queue**, with the stage/percent/ETA appended to that row's meta line.
- The browser tab title becomes `43% · photodump` while rendering.

### The node status line has more to say

Old: `desktop asleep · 3 queued, will drain on wake`. New lines, all real:

- `node ready · 0 queued`
- `rendering job #71 · 2 queued · 1 scheduled · 1 paused`
- `ComfyUI unavailable · 3 queued · retrying`: this replaced "asleep". **The
  app can't tell a sleeping PC from a PC that's awake but not running ComfyUI**,
  and the second case once cost me a day. The status now carries a
  `connection_error` string, either *"Cannot connect to ComfyUI. Check the
  desktop's ComfyUI service and firewall."* or *"ComfyUI connection timed out.
  Check that ComfyUI is running and reachable over Tailscale."* Today it's a
  hover tooltip on the status line, plus the small hint above each task's
  Generate button. It belongs in the node card as a quiet secondary line that
  still doesn't read as an alarm.
- `queue paused · 3 queued`: the queue-level pause (§3) overrides the others.
- `Connection lost · reconnecting…`: the photodump server itself is
  unreachable (rare).

"Scheduled" and "paused" jobs are **not waiting on the node**, so they're named
separately. Otherwise an idle node with five held jobs looks stuck.

### Preflight (added after the desktop artboard, never designed)

Under the node card, a collapsed `<details>` line:

- all good: `7/7 workflows ready on the node`
- broken: `2 of 7 workflows blocked — click for detail`

Expanded, one row per workflow with a `ready`/`blocked` tick, its label, and
why lines in mono, e.g.
`missing nodes: UnetLoaderGGUF, CLIPLoaderGGUF` or
`ckpt_name "waiNSFWIllustrious_v14.safetensors" not among the node's 3 option(s)`.
Only shown when the node is online. It's a diagnostic I check rarely but trust
completely. It should be quiet when green and obvious when red.

---

## 3. Queue: pause, hold, stop, resume, requeue, scheduling

The queue used to have one action (cancel). It is now a real control surface.

**New status: `paused`**, a warm amber pill, deliberately not grey like
`cancelled`. The job is still alive and waiting for me. Full set:
`queued` `running` `paused` `done` `failed` `cancelled`.

**Per-row actions by status:**

| Status | Actions |
|---|---|
| running | **pause** · **stop** |
| queued | **hold** · **stop** |
| paused | **resume** (amber) · **stop** |
| failed / cancelled | **requeue** (amber) · **copy error** (if there is one) |

What they mean, because the copy has to carry it:
- **pause** (running): stops sampling **and frees the GPU's VRAM** (that's the
  point: a 10–12GB checkpoint otherwise stays loaded). Not instant; the toast
  says *"Job #71 stopping at the node…"* for the couple of seconds in between.
- **hold** (queued): same `paused` status, just never started.
- **stop**: terminal.
- **resume**: back to queued. Also clears any schedule, so resume means "now".
- **requeue**: resets a failed job with attempts back to 0. Used after I fix
  whatever broke the graph.

**Queue-level pause:** a `pause queue` / `resume queue` text button in the
Queue section header. Whatever is rendering finishes; nothing new starts. When
on, it overrides the node status line (`queue paused · …`). Right now it's a
bare link button next to `3 queued · 1 running · 2 failed`. It's a
significant mode and should look like one.

**Row meta line** now carries: kind · aspect · `attempt 3` (when >1) ·
`scheduled 2026-09-15 06:00 UTC` · live progress (running row only).

**Kinds** to label: `create`, `fuse`, `extend`, `animate`, `reel`,
`Instagram encode`. Today Create jobs are **mislabelled "fuse"**, and the
two jobs of a style-match chain aren't linked in any way. Give me a treatment
that marks pass 1 and pass 2 as one request (e.g. `#69 → #70`).

**Scheduling** is built in the backend. A job can be held until a UTC time
(`POST /api/jobs/{id}/schedule`, `YYYY-MM-DD HH:MM`) but **the UI can only
display it, not set it**. Design the affordance, e.g. "hold until…" on a queued
row, with a quick pick like *tonight 1am* for renders that should run while I
sleep. Show times in local time; the API speaks UTC.

---

## 4. Toasts

A bottom-centre stack (above the tab bar on mobile), max width 440px. Every
queueing action confirms with one, and failures show up there instead of
`alert()`. Dot colour: **lime** = queued and the node is online; **teal** =
queued while the node is unavailable (*"Queued 4 renders · #71 — waiting for
ComfyUI to reconnect"*) or built on the server; **red** = failure (stays
7s) or a stop. Each has a × dismiss. Real examples:

- `Queued a clip from #58 · job #72`
- `Encoding #58 as reel · job #73`
- `Job #71 stopping at the node…`
- `Queue paused - the current render will finish`
- `Loaded the recipe from #44`
- `Deleted #61`
- `Upload failed: <server error text>` (red)
- `Cannot reach Photodump. Retrying…` (red)

Currently plain dark cards with a coloured dot. Fine, but unrefined, and they
need one consistent voice.

---

## 5. Detail view (lightbox): paging, Instagram export, reuse

### Paging
**‹ ›** buttons float over the left and right edges of the image stage;
**← →** keys page too. Disabled at the ends, hidden with fewer than 2 items.
Deleting keeps the lightbox open on the next image, so clearing out bad renders
takes one action per image. On mobile this probably wants **swipe**, and the
arrows should stay out of the way of the image.

### Make Instagram JPEG / mp4
A new action button opens an inline panel with the same shape as the Animate
panel:

- head: `instagram encode` + meta (`jpeg q95 · 1080 wide` for stills,
  `h.264 · 1080 wide · faststart` for clips)
- hint, which differs by media:
  - still: *"Crops when within 4% of the ratio (a 680×856 render loses 6px),
    pads otherwise."*
  - clip: *"Fits and pads — nothing is cropped."*
- three target buttons: **Reel 9:16 · Feed 4:5 · Square**

Picking one queues a job that runs **on the always-on server** (no GPU). Like
Reel, it works while the desktop sleeps, so give it the teal server-side
treatment Reel already has. The panels are exclusive: opening this closes
Animate.

### Reuse these settings
Shown only for images made from a Fuse recipe. Loads source A/B, blend and
extra direction into Fuse, closes the lightbox, switches to Fuse (and to the
Studio screen on mobile), and toasts. It should also apply to **Create** renders
(prompt, references, style-match settings), so design it generically.

### Animate panel changes
- Canvas options are now **six**, in pairs:
  `544×960 (9:16, fast)` · `704×1280 (9:16)` · `544×680 (4:5, fast)` ·
  `704×896 (4:5)` · `640×640 (1:1, fast)` · `960×960 (1:1)`, with the hint
  *"The 540 sizes render far faster on 16GB. Upscale after."* It's a flat
  `<select>` today; a ratio × speed picker would read better.
- The head meta still says **"still → .webm clip"**. That's stale: output is
  mp4 now.
- **Optional, backend-ready, no UI yet:** a video model choice. **WAN 2.2**
  (~20 min, 3–5s silent) vs **LTX-2.5** (~33–60 min, up to ~4s **with generated
  audio**, better motion). Seconds and time cost differ per model, so if you add
  the choice, show the cost next to it.

The action grid is now 7 buttons (Favourite, Draft caption, Download, Delete,
Animate, Make Instagram …, Reuse). It has outgrown its 2-column layout.
Please re-group it: *keep* (favourite, download, delete) vs *make something from
this* (animate, encode, reuse, caption).

---

## 6. Reel: "shots from" (added after the desktop artboard)

In the Reel task, a segmented control **shots from: renders · my photos ·
both**, with the hint *"Reels build on kanto, so photos you upload work even
with no GPU at all."* While picking, uploaded photos join the gallery grid as
tiles with a small `upload` tag and their label as the caption (not `#id`). It's
in the mobile artboard's DOM but was never drawn on desktop.

---

## Artboards I need back

**Desktop 1440×900**
1. **Create, no references**: node ready, the default screen now.
2. **Create, one reference, style match on**: pass 2 fields visible, clean-up
   pad with 2–3 boxes drawn, node asleep/unavailable with 3 queued.
3. **Create, three references**: style-only mode, the disabled
   keep-composition state; also the IP-Adapter-missing amber warning.
4. **Create, clean-up pad in "Preview fill" state.**
5. **Node card, all progress states** on their own: measured at 43%,
   estimated, unknown, ready, unavailable with its connection error,
   queue paused. Plus preflight collapsed green, collapsed red and expanded red.
6. **Queue** with one of each: running (with inline bar), queued + scheduled,
   paused, failed (long error), cancelled, a style-match pass 1→2 pair, and the
   queue-paused header state. Include the "hold until…" scheduling interaction.
7. **Detail view**: a still with the Instagram encode panel open, paging
   arrows visible, the regrouped action set.
8. **Toasts**: the ok/teal/bad variants, two stacked.

**Mobile 390×844**
9. **Create**, one reference, style match on, scrolled to the clean-up pad:
   the finger-drawing case.
10. **Queue** with running progress and the per-row actions. Tap targets, not
    link-sized text buttons.
11. **Detail view** with swipe paging and the encode panel.
12. **Node strip** while rendering with progress: it's pinned on every
    screen, so the percent/stage/ETA have to fit in a strip.

---

## Appendix: element ids the code reads

`web/app.js` uses `getElementById`. **Every id must survive, exactly once in
the DOM**, on the element whose `.value`/`.checked`/`.textContent`/`.src` means
something. Nesting and every other class name are free, *except*: `.tabs`,
`.tabs.sub`, `.tab[data-tab]`, `.task[data-task]`, `.output`, `.active`,
`.picking`. Visibility is toggled with the `hidden` attribute, so keep
`[hidden]{display:none!important}`. Segmented controls are
`<div class="seg"><span data-value="…" class="on">` and code gives them a
`.value`.

Everything from the original brief still applies. **New since then:**

**Node card**
`#node-prog` `#prog-pct` `#prog-stage` `#prog-eta` `#node-fill` (a `<b>` inside
`.node-rule`, which toggles classes `measured`/`estimated`). Preflight:
`#preflight` `#pf-summary` `#pf-body` (rows `.pf-row.ok/.bad` are generated).

**Create task** (`.task[data-task="create"]`)
`#cr-prompt` `#cr-quality` `#cr-negative` `#cr-ref-count` `#cr-refs`
(tiles generated as `figure[data-pick]`, `.on` + `.n` badge) `#cr-ref-hint`
`#cr-ip-row` `#cr-ipweight-wrap` `#cr-ipweight` `#cr-denoise-wrap` `#cr-denoise`
`#cr-stylematch-wrap` `#cr-stylematch` `#cr-mode` `#cr-p2-row` `#cr-p2-add`
`#cr-p2-avoid` `#cr-clean-wrap` `#cr-clean-count` `#cr-clean` (gets
`.previewing`) `#cr-clean-img` `#cr-clean-boxes` (boxes generated as
`i[data-box]`, `.drawing` while dragging) `#btn-clean-preview`
`#btn-clean-clear` `#cr-hires-wrap` `#cr-hires` `#cr-aspect` (seg, cloned from
Fuse's `#aspect`) `#cr-count` `#btn-cr-preview` `#btn-create` `#cr-qty`
`#cr-preview`. Shared with Fuse: `#steps` `#cfg` `#checkpoint`.

**Reel**
`#re-source` (seg: `renders` / `uploads` / `both`), `#picking-bar`
`#picking-count` `#strip` `#strip-shots` `#strip-total`, `[data-build-reel]`.

**Queue**
`#queue-pause` `#queue-meta` `#jobs`. Rows are generated. Their buttons
carry `data-pause` `data-resume` `data-stop` `data-requeue` `data-copyerr`
(plus the original `data-cancel`); the inline bar is `.bar[data-bar="<id>"] > b`
and the live stage span is `[data-job-progress="<id>"]`.

**Tabs / badges**
`#gallery-badge` `#queue-tab-badge` `#gallery-meta` `#refs-meta` `#mnav-queue`;
`[data-queue-hint]` and `[data-animate-hint]` (multiple allowed, all get the
same text); `[data-gallery-only]`.

**Lightbox**
`#lb-id` `#lb-meta` `#lb-prev` `#lb-next` `#lb-deliver` (its first `<span>`
gets the label) `#lb-reuse` `#deliver-box` `#deliver-meta` `#deliver-hint`, with
target buttons as `[data-target="reel|feed|square"]` inside `#deliver-box`.
`#an-size` option values: `story_540` `story` `portrait_540` `portrait`
`square_540` `square`.

**Toasts**
`#toasts` (container; toasts generated as `.toast.ok|.teal|.bad` with a
`.dot`, a text span and a dismiss `<button>`).
