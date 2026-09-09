# photodump — design brief

> **Status: the desktop artboard is implemented.** `PhotodumpDesktop.dc.html`
> (Claude Design project `3c802983`) was ported into `web/` on 2026-09-09.
> `PhotodumpMobile.dc.html` in that project has **not** been ported — the
> current mobile layout is the desktop CSS reflowing at a 900px breakpoint.

For a visual pass on the front end. The app is **built, wired and working**;
this describes what the design layer may change freely and what it must not.

Feed `design-prompt.md` to the designer. Read this one before porting the
result back into `web/`.

## Context

One person making original anime mashup art for an Instagram page. Two use
contexts that pull in different directions:

- **Phone**, away from the desk — queueing ideas, fire-and-forget
- **Desktop**, at the machine — reviewing, curating, building reels

Neither is secondary. Unlike nihongo, this is not a mobile-first app.

## The contract with the code

**`web/app.js` reads the DOM through `getElementById`.** This is a different
convention from nihongo and dylanime — get it wrong and things break silently.

### Element IDs are load-bearing

Every id below is read by JS. Renaming or dropping one breaks that feature with
no error until the moment it's used.

**Header / node status**

```
#node-dot  #node-text  #queue-badge
```

**Fuse form**

```
#subject-a  #subject-b  #mode  #mode-hint  #extra  #negative  #aspect  #count  #ref  #workflow  #denoise  #ip-weight  #steps  #cfg  #checkpoint  #btn-preview  #btn-generate  #preview  #starters  #btn-save-recipe  #recipes
```

**Extend form**

```
#ex-ref  #ex-target  #ex-anchor  #ex-prompt  #ex-feather  #ex-count  #btn-extend
```

**Reel form**

```
#sel-count  #sel-clear  #sel-favs  #re-timing  #re-bpm-row  #re-bpm  #re-beats  #re-secs-row  #re-seconds  #re-motion  #re-transition  #re-audio  #re-length  #btn-reel
```

**Gallery / queue / refs**

```
#only-fav  #grid  #gallery-empty  #jobs  #ref-form  #ref-file  #ref-label  #ref-kind  #ref-grid
```

**Detail view (lightbox)**

```
#lightbox  #lb-img  #lb-vid  #lb-prompt  #lb-caption  #lb-fav  #lb-caption-btn  #lb-animate  #lb-download  #lb-delete  #lb-close
```

**Animate panel**

```
#animate-box  #an-prompt  #an-seconds  #an-size  #an-go
```

Structure is otherwise free — these do **not** need to keep their current
nesting. If an element is split or merged, keep the id on whichever node should
receive the value. For inputs, the id must stay on the element whose `.value`
is meaningful.

### Some class names are load-bearing too

This is the trap. Most classes are free, but these are queried or toggled:

| Selector | Used for |
|---|---|
| `.tabs` | output tab row — click delegation |
| `.tabs.sub` | studio task row (Fuse/Extend/Reel) — click delegation |
| `.tab` + `data-tab` | output panes, shown/hidden by tab |
| `.task` + `data-task` | studio panes, shown/hidden by task |
| `.output` | gets `.picking` toggled in reel mode |
| `.active` | applied to the selected tab/task button |
| `.picking` | applied to `.output` while reel selection is on |

Everything else — `.panel`, `.studio`, `.grid`, `.chip`, `.job`, `.hint`,
`.badge`, `.star`, `.pick`, colours, the whole stylesheet — is free.

### data-* attributes are load-bearing

`data-tab` `data-task` `data-starter` `data-recipe` `data-img` `data-ref`
`data-del` `data-cancel`

Several are written by JS into generated markup (`data-img`, `data-recipe`,
`data-ref` carry JSON), so their **container element** matters more than their
styling. Click handlers are delegated via `closest()`, so nesting inside the
tile is fine.

### Visibility uses the `hidden` attribute

Panes are toggled with `el.hidden = true/false`, not `display`. Any CSS that
sets `display` on `.tab`, `.task`, `#animate-box`, `#preview`, `#lb-img`,
`#lb-vid`, `#re-bpm-row` or `#re-secs-row` must not override `[hidden]`. Keep
a `[hidden] { display: none !important; }` rule.

## States that must be designed, not just the happy path

This is where a normal gallery design would fail:

1. **Render node asleep** — the *default* state, not an error. Jobs queue and
   drain on wake. Must read as calm and expected.
2. **Node rendering** — a job in flight, with the queue depth behind it.
3. **Empty gallery** — first run, nothing rendered.
4. **Failed job** — shows a long technical error where the prompt would be.
   Design for overflow.
5. **Reel selection mode** — gallery tiles become ordered, numbered picks.
6. **IP-Adapter unavailable** — that dropdown option is disabled and its label
   is rewritten when the render node lacks the node pack.
7. **Clips vs stills** — the gallery mixes tall stills, `.webm` clips and
   `.mp4` reels. Clips currently autoplay on hover, which **has no mobile
   equivalent** — that needs a real answer.

## Known weak points in the current UI

Fair game to fix, no attachment:

- **Node status is a dot and grey text in the header.** It's the most
  consulted element in the app and looks like an afterthought.
- **Reel selection is invisible from the studio panel** — you select in the
  gallery on the right but the controls are on the left, and on mobile they're
  in different stacked sections entirely.
- **The "Reference image & sampler" `<details>`** hides seven controls behind
  one summary line; there's no indication anything inside is set.
- **The tag preview `<pre>`** is a wall of grey monospace.
- **Nothing communicates that reels build on the always-on server** and
  therefore work while the desktop sleeps.
- No mobile answer for hover-to-play clips.

## Porting the result back

Follow the NERV workflow: **port visuals only, keep the real-data wiring.**
Take markup and CSS from the artboards; keep every id, the load-bearing classes
above, the `data-*` hooks, and `hidden`-based visibility. Then run:

```sh
docker compose up -d --build     # code is baked in; restart ships nothing
./tools/smoke.sh                 # 16 assertions, backend only - see below
```

`smoke.sh` exercises the API and worker, **not the DOM** — it will pass with a
completely broken front end. Verify the UI by hand: queue a render with the
mock running, switch all three tasks, select for a reel, open the detail view.
