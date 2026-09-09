Design the UI for **photodump**, a studio for making original anime mashup art
for an Instagram page. It's a working web app — I need the visual design, as
artboards I can build from.

## What it is

A single-person creative tool. I generate original anime art by blending two
franchises (Pokémon × Jujutsu Kaisen, say), extend photos onto Instagram
canvases, animate stills into short clips, and assemble stills into reels with
music. Then I post the good ones.

The images are rendered on the GPU in my desktop PC. **The web app runs on a
different machine — a server that's always on — and my desktop is asleep most
of the time.** That fact drives the entire design and I'll come back to it.

## Who's using it and where

Me, alone. Two genuinely different contexts, and this is not a
mobile-first-desktop-afterthought app — both matter:

- **Phone, away from my desk.** Queueing ideas. I think "gardevoir as a jujutsu
  sorcerer" on the couch, type it, queue four variations, put the phone down.
  Fire-and-forget. Design for a **390×844 iPhone viewport**.
- **Desktop, at the machine.** Reviewing what rendered overnight, favouriting,
  building a reel, drafting captions. This is a **1440×900** two-column working
  session with a lot of images on screen.

Design both. The phone case is mostly *input*; the desktop case is mostly
*looking at pictures*.

## The thing that makes this app different

**Most of the time, nothing can render right now.** My desktop is asleep. Jobs
sit in a queue and drain whenever I next wake the PC — could be minutes, could
be tomorrow.

This is **normal operation, not an error state**, and it's the single most
important thing to get right. Every image-generation UI I've seen assumes you
click and get a picture. Here you click and get a *promise*. Waiting has to feel
calm and intentional — not like something broke, not like a spinner that's been
stuck for six hours.

There are three states and they need to read differently at a glance:

| State | What's true | Current copy |
|---|---|---|
| **asleep** | Desktop off. Queue accepting work. | `desktop asleep · 3 queued, will drain on wake` |
| **ready** | Desktop on, nothing running. | `node ready · 0 queued` |
| **rendering** | A job is in flight right now. | `rendering job #12 · 2 queued` |

Right now this is a coloured dot and a line of grey text in the header. It
deserves better — it's the first thing I look at every single time I open the
app. But don't make it alarming: "asleep" is the *expected* state, not a fault.

**Reels are the exception** — they're assembled on the always-on server, so a
reel builds even when the desktop is asleep. Worth expressing somehow, because
it means "Reel" is the one task that always works immediately.

## The screens

Everything lives on **one page**: a control panel on the left, output on the
right. On mobile it stacks. Keep that shape or propose better.

### Left: the studio panel — three tasks

Switched by a small tab row: **Fuse · Extend · Reel**. Only one visible at a time.

---

**Task 1 — Fuse** (the main one, ~70% of use)

Blend two sources into one original design. The form:

- **Source A** — e.g. `gardevoir`
- **Source B** — e.g. `gojo satoru`
- **Blend** — a dropdown of four modes, each with a one-line explanation
  underneath that changes with the selection:
  - *Design fusion* — "one character redesigned in the other's visual language. The workhorse."
  - *Crossover scene* — "both characters together in one shot. Best for narrative posts."
  - *World transplant* — "subject A rendered inside B's world and art direction."
  - *Original character* — "a new character carrying DNA from both. Most 'original' output."
- **Extra direction** — free text, e.g. `blindfold, cursed energy, neon rain`
- **Avoid** — free text
- **Canvas** (portrait 4:5 / square / story 9:16 / landscape) and **Count** (1–8)
- A collapsed *"Reference image & sampler"* section holding: reference picker,
  how-to-use-it dropdown, denoise, IP weight, steps, CFG, checkpoint
- Two buttons: **Preview tags** (secondary) and **Queue render** (primary)

"Preview tags" reveals the compiled prompt. It's long, ugly, technical, and I
do want to see it — but it should feel like opening the hood, not like the main
event. Real example of what appears:

```
masterpiece, best quality, amazing quality, very aesthetic, absurdres, highres,
1girl, solo, original character, gardevoir redesigned as gojo satoru, character
design fusion, wearing gojo satoru styled outfit, gojo satoru colour palette,
full body, standing, simple background, character sheet lighting, blindfold,
white hair, cursed energy, glowing blue eyes
```

Below the form: **Starters** — one-tap chips that fill the whole form. Real ones:
`Gardevoir x Gojo` · `Lucario x Sukuna` · `Pikachu in Shibuya` ·
`Gengar curse spirit` · `Nobara x Sylveon`

Then **saved recipes** — same chip treatment, each with a small delete affordance.

---

**Task 2 — Extend**

Grow a photo onto a new canvas. The model invents the new territory; my original
pixels are never cropped. That reassurance matters — say it in the UI.

Fields: source image picker, extend-to ratio, where to anchor the original
(centred / top / bottom), a scene description, feathering, count.

One piece of guidance that needs to be visible, because getting it wrong gives
bad results: **describe the finished picture, not just the new parts.** The
sampler is filling in *around* what's already there.

---

**Task 3 — Reel**

Assemble stills into a 1080×1920 video with music.

Selection happens **in the gallery on the right** — clicking a still adds it,
in order, with a numbered badge. So this panel is mostly settings plus a
running summary:

- `4 selected` with **clear** and **use favourites** buttons
- Cut timing: *on the beat (BPM)* or *fixed seconds*. BPM mode shows BPM +
  beats-per-shot; fixed mode shows a seconds field.
- Motion: Ken Burns / static. Between: hard cut / crossfade.
- Music picker
- A live length estimate: `4 shots × 1.88s = 7.5s`
- **Build reel** (primary)

The ordered-selection interaction is the interesting design problem here.

### Right: output

A tab row: **Gallery · Queue · References**.

**Gallery** — a grid of everything rendered. Mixed content:
- stills (tall, 4:5 or 9:16 — the grid must handle **vertical** images well;
  this is not a square-photo app)
- clips (`.webm`, currently autoplay-on-hover — needs a mobile answer, there's no hover)
- reels (`.mp4`, 1080×1920)

Each tile shows an id + seed caption, a ★ if favourited, and — when reel mode
is on — a numbered selection badge. There's a "favourites only" filter.

Tapping a tile opens a **detail view**: the full image, the prompt it came from,
caption/hashtags if drafted, and actions — Favourite, Draft caption, **Animate**,
Download, Delete, Close. "Animate" expands an inline panel: motion description,
seconds, canvas, and a queue button. (Animate only applies to stills — a clip
can't be animated again.)

Caption output looks like this, and both parts need styling:

> Gardevoir with six eyes and a grudge. Nobody's teaching this one Dazzling Gleam.
>
> #anime #animeart #jujutsukaisen #pokemon #gojosatoru #gardevoir #crossover
> #fanart #animefusion #aiart #originalcharacter #animeedit

**Queue** — a list of jobs. Each row: a status pill, the prompt (truncated), and
a cancel button while it's still queued. Statuses: `queued` `running` `done`
`failed` `cancelled`. A failed job shows its error instead of its prompt, and
the errors are long and technical — design for text that overflows.

**References** — an upload form (file, label, kind: character/style/pose/audio)
and a square grid of uploaded reference images.

## Style direction

- **Dark by default.** It's an art tool; the work is saturated anime art and it
  should pop off the chrome. I'm often using it at night.
- **The images are the product.** Chrome recedes. The current build uses a
  violet/pink accent pair which I like, but I'm not attached.
- Type: nothing precious. This is a workbench.
- It should feel like a **studio tool**, not a social app and not a
  consumer AI toy. Closer to a darkroom than to Instagram itself.
- The name is lowercase: **photodump**.

## What I need back

Artboards for:

1. **Desktop — Fuse**, node asleep with 3 jobs queued (the most common state)
2. **Desktop — Fuse**, node rendering, with the tag preview open
3. **Desktop — Gallery**, populated, mixed stills and a reel
4. **Desktop — Reel mode**, gallery in selection state with 4 numbered picks
5. **Desktop — detail view** with the animate panel open and a caption drafted
6. **Desktop — Queue**, including a failed job with a long error
7. **Desktop — Extend**
8. **Mobile 390×844 — Fuse**, node asleep (the couch-queueing case)
9. **Mobile — Gallery**
10. **Mobile — detail view**

Plus the three node-status treatments called out on their own, since that
component carries more weight than its size suggests.
