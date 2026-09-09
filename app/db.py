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
    status      TEXT NOT NULL DEFAULT 'queued',  -- queued|running|done|failed|cancelled
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


def init():
    with db() as conn:
        conn.executescript(SCHEMA)


def rows(cur):
    return [dict(r) for r in cur.fetchall()]


def loads(s, default=None):
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return default if default is not None else {}
