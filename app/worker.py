"""Queue drainer.

The desktop is a laptop-shaped dependency: awake when Dylan is at it, asleep
otherwise. So the worker never fails a job for being unable to reach the node -
it just leaves it queued and tries again. Jobs drain when the PC wakes.
"""
import asyncio
import json
import uuid

from PIL import Image

from . import comfy, imageops
from .config import DATA, OUT, REFS, THUMBS
from .db import db, loads

POLL_IDLE = 20     # nothing to do / node asleep
POLL_ACTIVE = 2    # a render is in flight

_state = {"online": False, "current": None, "last_error": ""}


def status() -> dict:
    with db() as conn:
        q = conn.execute("SELECT COUNT(*) c FROM jobs WHERE status='queued'").fetchone()["c"]
    return {**_state, "queued": q}


async def _claim():
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1"
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE jobs SET status='running', started_at=datetime('now') WHERE id=?",
            (row["id"],),
        )
        return dict(row)


def _release(job_id: int):
    """Put a job back on the queue - used when the node vanishes mid-flight."""
    with db() as conn:
        conn.execute(
            "UPDATE jobs SET status='queued', started_at=NULL WHERE id=? AND status='running'",
            (job_id,),
        )


def _fail(job_id: int, msg: str):
    with db() as conn:
        conn.execute(
            "UPDATE jobs SET status='failed', error=?, finished_at=datetime('now') WHERE id=?",
            (msg[:1000], job_id),
        )


def _thumb(name: str):
    try:
        with Image.open(OUT / name) as im:
            im.thumbnail((512, 512))
            im.convert("RGB").save(THUMBS / f"{name}.jpg", "JPEG", quality=85)
    except Exception:
        pass  # a missing thumb degrades the grid, it does not break the job


async def _run(job: dict):
    params = loads(job["params"])
    ref_name = None

    if job["ref_id"]:
        with db() as conn:
            ref = conn.execute("SELECT * FROM refs WHERE id=?", (job["ref_id"],)).fetchone()
        if ref:
            src = REFS / ref["filename"]
            if params.get("workflow") == "outpaint":
                # Pads depend on the real pixel dimensions, so they are computed
                # here rather than at queue time - the image is on disk now.
                prepped = DATA / f".prep_{job['id']}.png"
                pads = imageops.prepare(
                    src, prepped,
                    params.get("extend_target", "portrait"),
                    params.get("extend_anchor", "center"),
                )
                params["pads"] = pads
                # Write the resolved geometry back, so the job record shows what
                # was actually rendered rather than what was requested.
                with db() as conn:
                    conn.execute("UPDATE jobs SET params=? WHERE id=?",
                                 (json.dumps(params), job["id"]))
                src = prepped
            ref_name = await comfy.upload_image(src)

    graph, seed = comfy.build(job["prompt"], job["negative"], params, ref_name)
    prompt_id = await comfy.submit(graph, str(uuid.uuid4()))

    # Poll history until the node reports outputs for our prompt.
    for _ in range(900):  # 30 min ceiling at 2s
        await asyncio.sleep(POLL_ACTIVE)
        hist = await comfy.history(prompt_id)
        if not hist:
            continue
        st = hist.get("status", {})
        if st.get("status_str") == "error":
            raise comfy.ComfyError(json.dumps(st.get("messages", []))[:600])
        outputs = hist.get("outputs") or {}
        if not outputs:
            continue

        saved = 0
        for node in outputs.values():
            for img in node.get("images", []):
                data = await comfy.fetch(img["filename"], img.get("subfolder", ""), img.get("type", "output"))
                name = f"{job['id']}_{seed}_{saved}.png"
                (OUT / name).write_bytes(data)
                _thumb(name)
                with db() as conn:
                    conn.execute(
                        "INSERT INTO images (job_id, filename, seed) VALUES (?,?,?)",
                        (job["id"], name, seed),
                    )
                saved += 1

        if saved:
            with db() as conn:
                conn.execute(
                    "UPDATE jobs SET status='done', finished_at=datetime('now') WHERE id=?",
                    (job["id"],),
                )
            return
    raise comfy.ComfyError("timed out waiting for the render node")


async def loop():
    while True:
        h = await comfy.health()
        _state["online"] = h["online"]
        if not h["online"]:
            _state["current"] = None
            await asyncio.sleep(POLL_IDLE)
            continue

        job = await _claim()
        if not job:
            _state["current"] = None
            await asyncio.sleep(POLL_IDLE)
            continue

        _state["current"] = job["id"]
        try:
            await _run(job)
            _state["last_error"] = ""
        except comfy.ComfyOffline as e:
            # Not a failure. The PC went to sleep; requeue and wait it out.
            _release(job["id"])
            _state["online"] = False
            _state["last_error"] = f"node went away: {e}"
            await asyncio.sleep(POLL_IDLE)
        except Exception as e:
            _fail(job["id"], str(e))
            _state["last_error"] = str(e)
        finally:
            _state["current"] = None
