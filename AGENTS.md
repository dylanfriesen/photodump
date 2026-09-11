# photodump — working agreement

Read by both Codex (`AGENTS.md`) and Claude Code (`CLAUDE.md`, a symlink to this
file, so they cannot drift).

---

## Record every substantial commit

**After any commit that adds a feature, changes behaviour, or fixes something
non-obvious, add a short entry to `PROGRESS.md`** under today's date, above the
`## Log` section.

`.git/hooks/post-commit` already appends a one-line record of *every* commit
automatically — that half is handled and needs nothing from you. What it cannot
capture is the part worth keeping:

- **why** the change was made
- **what it cost** — the wrong turn, the bug you introduced and caught, the
  assumption that turned out false
- **what is now unproven or still open**

One short paragraph. Write the thing a stranger would need to not repeat your
mistake. Skip it for typos and doc tweaks; the automatic line covers those.

Claude should also write a memory entry for anything that changes the project's
shape, so it survives into sessions that never read this repo.

## Do not test against the live instance

The live app is `docker compose up -d` on port **8096** with the real database
at `data/photodump.db`. It holds queued jobs and the gallery.

For anything experimental, run an isolated copy:

```sh
CONTAINER_NAME=photodump-test PORT=8097 DATA_DIR=/srv/data/_test \
  COMFY_HOST=mock-comfy docker compose -p photodump-test up -d
```

`tools/smoke.sh` already does this. **Never `rm` anything under `data/` on the
live instance** — deleting `photodump.db` destroyed queued jobs on 2026-09-10,
and the schema is only created at startup, so the app 500s until restarted.

## Invariants that look like over-engineering and are not

- **`ComfyOffline` vs `ComfyError`** (`app/comfy.py`). Unreachable node →
  requeue. Rejected graph → fail. Collapse them and every job fails the moment
  the desktop sleeps.
- **`MAX_ATTEMPTS = 5`** — a sleeping PC and a ComfyUI crashing on one specific
  graph are indistinguishable from kanto. Without the cap the second loops
  forever.
- **Reel and delivery jobs never gate on node health** — they run on kanto with
  ffmpeg and must work while the desktop is off.
- **WAN frame count must be 4n+1**, or the final chunk degrades silently.
- **Workflow JSON node ids are API.** `comfy.py` indexes graphs by string key.
  Renumber a graph and the builder breaks with a `KeyError` that surfaces as a
  requeue, not a clear failure.
- **fp8 (e4m3fn) is broken on RDNA4/Windows.** fp16 or GGUF only.

## After cloning

`.git/hooks` is not versioned, so run **`./tools/install-hooks.sh`** once to get
the post-commit progress logger. Without it, commits stop being recorded and
nobody notices.

## Mechanics

- Code is baked into the image: **`docker compose up -d --build`**. A plain
  `restart` ships nothing, and a stale image has wasted time three times now.
- `data/` and `.env` are gitignored. The repo is **public** — no personal
  addresses or credentials in tracked files.
- Tests: `./tools/smoke.sh` (24 assertions, no GPU) and
  `python -m unittest tools.test_mailer tools.test_progress`, run with `tools/`
  mounted since the image does not ship it.

## Recipes

**`RECIPES.md` holds proven parameter sets**, with the reasoning behind each
number. Read it before tuning generation quality — the values there were
established by sweeps, and the "what does not work" notes record approaches
already tried and ruled out. Add to it when a sweep settles something.

## Current status

`COMPLETION-NOTES.md` is the live handoff. `AT-THE-DESKTOP.md` lists what needs
doing in person. `HANDOFF.md` and `SESSION-SUMMARY.md` are historical and carry
corrected-but-dated status notes.
