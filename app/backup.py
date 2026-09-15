"""Nightly snapshot of the database and every file it points at.

Runs inside the live container (`python -m app.backup`), because `data/` is
written as root and a host-side copy cannot read the WAL sidecars.

Why it exists: deleting `photodump.db` on 2026-09-10 destroyed queued jobs,
and nothing could bring them back.

* The database is copied with SQLite's online backup API, never `cp`. The app
  writes in WAL mode, so a file copy mid-write is a torn snapshot that opens
  fine and is missing whatever was still in the -wal file.
* `refs/` and `out/` are **hardlinked**, not copied. Every file there has a
  unique name and is never rewritten in place, so a hardlink survives the app
  (or a person) deleting the original, and costs no disk. It does not survive
  the disk itself dying; that needs an off-box copy, which this is not.
* Snapshots older than KEEP days are pruned, and pruning only ever touches
  directories whose names parse as dates, so a stray file is never deleted.
"""
import os
import shutil
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import DATA, DB_PATH, OUT, REFS

ROOT = DATA / "backups"
KEEP = 14


def _link_tree(src: Path, dst: Path) -> int:
    dst.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in src.iterdir():
        if not f.is_file():
            continue
        target = dst / f.name
        if target.exists():
            continue
        try:
            os.link(f, target)
        except OSError:
            shutil.copy2(f, target)    # different filesystem: copy instead
        n += 1
    return n


def snapshot(day: date | None = None, root: Path = ROOT) -> dict:
    day = day or date.today()
    dst = root / day.isoformat()
    dst.mkdir(parents=True, exist_ok=True)
    tmp = dst / "photodump.db.partial"
    tmp.unlink(missing_ok=True)

    src = sqlite3.connect(DB_PATH, timeout=30)
    out = sqlite3.connect(tmp)
    try:
        with out:
            src.backup(out)
        ok = out.execute("PRAGMA integrity_check").fetchone()[0]
        jobs = out.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        images = out.execute("SELECT COUNT(*) FROM images").fetchone()[0]
    finally:
        out.close()
        src.close()
    if ok != "ok":
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"snapshot failed integrity_check: {ok}")
    tmp.replace(dst / "photodump.db")

    return {
        "dir": str(dst),
        "jobs": jobs,
        "images": images,
        "refs_linked": _link_tree(REFS, dst / "refs"),
        "out_linked": _link_tree(OUT, dst / "out"),
    }


def prune(keep: int = KEEP, root: Path = ROOT, today: date | None = None) -> list[str]:
    cutoff = (today or date.today()) - timedelta(days=keep - 1)
    gone = []
    for d in sorted(root.iterdir()) if root.exists() else []:
        try:
            when = datetime.strptime(d.name, "%Y-%m-%d").date()
        except ValueError:
            continue
        if d.is_dir() and when < cutoff:
            shutil.rmtree(d)
            gone.append(d.name)
    return gone


def main() -> int:
    info = snapshot()
    pruned = prune()
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} snapshot {info['dir']}: "
          f"{info['jobs']} jobs, {info['images']} images, "
          f"+{info['refs_linked']} refs, +{info['out_linked']} outputs; "
          f"pruned {pruned or 'nothing'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
