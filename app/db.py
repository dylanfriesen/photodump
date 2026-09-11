import json
import sqlite3
from contextlib import contextmanager

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS refs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    filename   TEXT NOT NULL,
    label      TEXT NOT NULL DEFAULT '',
    kind       TEXT NOT NULL DEFAULT 'character',  -- character | style | pose
    notes      TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS recipes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    subject_a  TEXT NOT NULL DEFAULT '',
    subject_b  TEXT NOT NULL DEFAULT '',
    mode       TEXT NOT NULL DEFAULT 'design_fusion',
    extra      TEXT NOT NULL DEFAULT '',
    negative   TEXT NOT NULL DEFAULT '',
    params     TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    recipe_id   INTEGER,
    prompt      TEXT NOT NULL,
    negative    TEXT NOT NULL DEFAULT '',
    params      TEXT NOT NULL DEFAULT '{}',
    ref_id      INTEGER,
    status      TEXT NOT NULL DEFAULT 'queued',  -- queued|running|paused|done|failed|cancelled
    error       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    started_at  TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS images (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     INTEGER NOT NULL,
    filename   TEXT NOT NULL,
    seed       INTEGER NOT NULL DEFAULT 0,
    favourite  INTEGER NOT NULL DEFAULT 0,
    caption    TEXT NOT NULL DEFAULT '',
    hashtags   TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_images_job ON images(job_id);
"""


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# Columns added after the first release. SQLite has no "ADD COLUMN IF NOT
# EXISTS", so each is attempted and its duplicate error swallowed.
MIGRATIONS = [
    "ALTER TABLE jobs ADD COLUMN src_image_id INTEGER",
    "ALTER TABLE jobs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE jobs ADD COLUMN ref_ids TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE refs ADD COLUMN width INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE refs ADD COLUMN height INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE jobs ADD COLUMN prompt_id TEXT NOT NULL DEFAULT ''",
    # Pause/stop are requests, not states: the API writes one here and the
    # worker acts on it at its next poll. A state would race with the worker,
    # which owns `status` for the job it is rendering.
    "ALTER TABLE jobs ADD COLUMN control TEXT NOT NULL DEFAULT ''",
    # Scheduling. NULL means "as soon as possible"; _claim skips the rest.
    "ALTER TABLE jobs ADD COLUMN not_before TEXT",
    # Video resume: the input-dir filename of the furthest checkpoint latent
    # and the step it was taken at. Empty means "start from step 0".
    "ALTER TABLE jobs ADD COLUMN resume_latent TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE jobs ADD COLUMN resume_step INTEGER NOT NULL DEFAULT 0",
]


def setting(conn, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key: str, value: str):
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def init():
    with db() as conn:
        conn.executescript(SCHEMA)
        for stmt in MIGRATIONS:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise


def rows(cur):
    return [dict(r) for r in cur.fetchall()]


def loads(s, default=None):
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return default if default is not None else {}
