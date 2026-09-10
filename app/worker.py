"""Queue drainer.

The desktop is a laptop-shaped dependency: awake when Dylan is at it, asleep
otherwise. So the worker never fails a job for being unable to reach the node -
it just leaves it queued and tries again. Jobs drain when the PC wakes.
"""
import asyncio
import json
import uuid
from pathlib import Path

from PIL import Image

from . import comfy, deliver, imageops, progress, reels
from .config import DATA, OUT, REFS, THUMBS
from .db import db, loads

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}

POLL_IDLE = 20     # nothing to do / node asleep
POLL_ACTIVE = 2    # a render is in flight
MAX_ATTEMPTS = 5   # requeue ceiling; see _release

_state = {"online": False, "current": None, "last_error": ""}


def status() -> dict:
    with db() as conn:
        q = conn.execute("SELECT COUNT(*) c FROM jobs WHERE status='queued'").fetchone()["c"]
    return {**_state, "queued": q, "progress": progress.snapshot()}


async def _claim(local: bool = False):
    """Claim the next queued job.

    `local` selects reel jobs, which render here on kanto. They are claimed
    without checking the node, since they never touch it. Node jobs are only
    ever claimed *after* a successful health probe - otherwise a week of the
    desktop being asleep would burn through MAX_ATTEMPTS on every job.
    """
    op = "IN" if local else "NOT IN"
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE status='queued' "
            f"AND COALESCE(json_extract(params, '$.workflow'), '') {op} ('reel','deliver') "
            "ORDER BY id LIMIT 1"
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE jobs SET status='running', started_at=datetime('now'), "
            "attempts=attempts+1 WHERE id=?",
            (row["id"],),
        )
        return dict(row)


def _release(job_id: int, reason: str = ""):
    """Put a job back on the queue - used when the node vanishes mid-flight.

    A sleeping PC is indistinguishable from a ComfyUI that crashes on this
    particular graph, and the latter would requeue forever. So requeues are
    capped: past MAX_ATTEMPTS the job is failed rather than left to spin.
    """
    with db() as conn:
        row = conn.execute("SELECT attempts FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row and row["attempts"] >= MAX_ATTEMPTS:
            conn.execute(
                "UPDATE jobs SET status='failed', finished_at=datetime('now'), error=? "
                "WHERE id=? AND status='running'",
                (f"gave up after {MAX_ATTEMPTS} attempts; last: {reason}"[:1000], job_id),
            )
            return
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


async def _run_reel(job: dict):
    """Assemble a reel from already-rendered stills. No render node involved."""
    params = loads(job["params"])
    # Shots may come from generated images or from uploaded references, mixed
    # and in any order, so they are addressed as {src, id} rather than a bare
    # id list. `image_ids` is still accepted for older jobs.
    shots = params.get("shots")
    if not shots:
        shots = [{"src": "image", "id": i} for i in (params.get("image_ids") or [])]

    paths = []
    with db() as conn:
        for shot in shots:
            sid = shot.get("id")
            if shot.get("src") == "ref":
                row = conn.execute("SELECT * FROM refs WHERE id=?", (sid,)).fetchone()
                if row and row["kind"] != "audio":
                    paths.append(REFS / row["filename"])
            else:
                row = conn.execute("SELECT * FROM images WHERE id=?", (sid,)).fetchone()
                if row and not row["filename"].lower().endswith((".webm", ".mp4")):
                    paths.append(OUT / row["filename"])
    paths = [p for p in paths if p.exists()]
    if len(paths) < 2:
        raise comfy.ComfyError("need at least two usable stills for a reel")

    audio = None
    if params.get("audio_ref_id"):
        with db() as conn:
            a = conn.execute("SELECT * FROM refs WHERE id=?", (params["audio_ref_id"],)).fetchone()
        if a:
            audio = REFS / a["filename"]

    name = f"{job['id']}_reel.mp4"
    await reels.build(
        paths, name,
        on_progress=progress.fraction,
        bpm=params.get("bpm"),
        beats_per_shot=int(params.get("beats_per_shot", 4)),
        seconds=float(params.get("seconds", 2.0)),
        motion=params.get("motion", "kenburns"),
        transition=params.get("transition", "cut"),
        audio=audio,
    )
    with db() as conn:
        conn.execute("INSERT INTO images (job_id, filename, seed) VALUES (?,?,0)",
                     (job["id"], name))
        conn.execute("UPDATE jobs SET status='done', finished_at=datetime('now') WHERE id=?",
                     (job["id"],))


async def _run_deliver(job: dict):
    """Re-encode an existing clip or reel for Instagram. No render node needed."""
    params = loads(job["params"])
    with db() as conn:
        row = conn.execute("SELECT * FROM images WHERE id=?",
                           (params.get("src_image_id"),)).fetchone()
    if not row:
        raise comfy.ComfyError("source clip no longer exists")

    name = f"{job['id']}_ig.mp4"
    info = await deliver.deliver(OUT / row["filename"], name,
                                 params.get("target", "reel"),
                                 on_progress=progress.fraction)
    params["result"] = info
    with db() as conn:
        conn.execute("INSERT INTO images (job_id, filename, seed) VALUES (?,?,0)",
                     (job["id"], name))
        conn.execute("UPDATE jobs SET status='done', finished_at=datetime('now'), "
                     "params=? WHERE id=?", (json.dumps(params), job["id"]))


async def _run(job: dict):
    previous = job.get("prompt_id")
    if previous:
        active = await comfy.pending_prompt(previous)
        if active:
            graph = active["graph"]
            client_id = active["client_id"] or str(uuid.uuid4())
            kind = "ltx_i2v" if any(str(n.get("class_type", "")).startswith("LTX") for n in graph.values()) else None
            progress.resume(active.get("created"), kind)
            progress.stage("reconnected; waiting for the next progress update")
            async with comfy.progress_socket(client_id, _progress_handler(graph, [previous])):
                await _await_outputs(previous, job, 0)
            return
        if await comfy.history(previous):
            await _await_outputs(previous, job, 0)
            return

    params = loads(job["params"])
    ref_name = None

    # Animating a previously generated image: the source lives in OUT, not REFS.
    if params.get("workflow") == "wan_i2v" and job["src_image_id"]:
        with db() as conn:
            img = conn.execute("SELECT * FROM images WHERE id=?", (job["src_image_id"],)).fetchone()
        if not img:
            raise comfy.ComfyError("source image no longer exists")
        ref_name = await comfy.upload_image(OUT / img["filename"])

    elif job["ref_id"]:
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

    client_id = str(uuid.uuid4())
    expected = [None]
    async with comfy.progress_socket(client_id, _progress_handler(graph, expected)):
        prompt_id = await _submit_once(job, graph, client_id)
        expected[0] = prompt_id
        await _await_outputs(prompt_id, job, seed)


async def _submit_once(job: dict, graph: dict, client_id: str) -> str:
    """Submit, unless this job's previous attempt is still live on the node.

    The desktop sleeping mid-render requeues the job here, but the node keeps
    its copy of the prompt and resumes it on wake. Resubmitting then renders
    the same job twice - it burns GPU time and lands duplicate images in the
    gallery. So an attempt that the node still recognises is re-attached to
    rather than repeated.

    Progress will not stream for a re-attached prompt: its step events are
    addressed to the client id of the original attempt, which is gone. The
    UI falls back to its time estimate, which is the honest thing to show
    for a render that started before this process was watching.
    """
    previous = job.get("prompt_id") or ""
    if previous:
        if previous in await comfy.pending_ids():
            progress.stage("resuming previous attempt on the node")
            return previous
        if await comfy.history(previous):
            return previous          # already finished; _await_outputs collects it

    prompt_id = await comfy.submit(graph, client_id)
    with db() as conn:
        conn.execute("UPDATE jobs SET prompt_id=? WHERE id=?", (prompt_id, job["id"]))
    return prompt_id


def _progress_handler(graph: dict, expected=None):
    """Translate ComfyUI's execution events into the progress store.

    Only the sampler reports a step count, so `progress` drives the bar and
    `executing` names the phase. Everything after the last step - decode,
    video encode, save - has no step count anywhere in ComfyUI, so it is
    reported as a named tail stage rather than a number.
    """
    sampler_nodes = {str(k) for k, v in graph.items()
                     if v.get("class_type") in {"KSampler", "KSamplerAdvanced", "SamplerCustomAdvanced"}}
    visited = []
    current_node = None

    def sampler_phase(node):
        # Count upstream sampler nodes, so reconnecting during LTX's second
        # pass does not mislabel it as pass one just because we missed pass one.
        seen = set()
        def walk(key):
            if key in seen:
                return
            seen.add(key)
            for value in graph.get(key, {}).get("inputs", {}).values():
                if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                    walk(value[0])
        walk(node)
        return len((seen & sampler_nodes) - {node})

    def handle(kind: str, data: dict):
        nonlocal current_node
        if expected and expected[0] and data.get("prompt_id") not in (None, expected[0]):
            return
        if kind == "progress":
            node = str(data.get("node") or current_node or "")
            if node in sampler_nodes and node not in visited:
                visited.append(node)
            total = max(1, len(sampler_nodes))
            phase = sampler_phase(node) if node in sampler_nodes else max(0, len(visited) - 1)
            progress.step(data.get("value", 0), data.get("max", 0),
                          phase=phase, phases=total,
                          stage=f"sampling pass {phase + 1}/{total}" if total > 1 else "sampling")
        elif kind == "executing":
            node = data.get("node")
            current_node = str(node) if node is not None else None
            if current_node in sampler_nodes and current_node not in visited:
                visited.append(current_node)
            if node is None:
                progress.stage("fetching result", tail=True)
            else:
                name = comfy.stage_for(graph, node)
                progress.stage(name, tail=bool(visited) and sampler_phase(visited[-1]) >= len(sampler_nodes) - 1 and
                               name in ("decoding", "decoding video", "decoding audio", "encoding video", "saving", "saving video"))
    return handle


async def _await_outputs(prompt_id: str, job: dict, seed: int):
    """Poll history until the node reports outputs, then pull them down."""
    polls = 0
    while True:
        await asyncio.sleep(POLL_ACTIVE)
        polls += 1
        hist = await comfy.history(prompt_id)
        if not hist:
            # Slow multi-pass video can exceed 30 minutes. An acknowledged
            # active prompt must not fail merely because it is still rendering.
            if polls % 900 == 0 and prompt_id not in await comfy.pending_ids():
                raise comfy.ComfyError("render disappeared from the node without outputs")
            continue
        st = hist.get("status", {})
        if st.get("status_str") == "error":
            raise comfy.ComfyError(json.dumps(st.get("messages", []))[:600])
        outputs = hist.get("outputs") or {}
        if not outputs:
            continue

        saved = 0
        for node in outputs.values():
            files = []
            for key in ("images", "gifs", "videos"):
                files.extend(node.get(key, []))
            for img in files:
                if not isinstance(img, dict) or "filename" not in img:
                    continue
                data = await comfy.fetch(img["filename"], img.get("subfolder", ""), img.get("type", "output"))
                ext = Path(img["filename"]).suffix.lower() or ".png"
                name = f"{job['id']}_{seed}_{saved}{ext}"
                (OUT / name).write_bytes(data)
                if ext in IMAGE_EXT:
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
            # WebSocket teardown can wait for a close handshake. The render
            # is already saved, so stop showing it as active before that wait.
            _state["current"] = None
            progress.done()
            return


def recover():
    """Requeue whatever was in flight when this process last stopped.

    `running` means "a worker in this process owns it". After a restart that
    is false for every such row, and nothing ever claims them again - the
    queue shows a job rendering forever while the node sits idle. Attempts
    are deliberately not reset: a restart loop should still hit the ceiling.

    Re-attachment makes this cheap. If the node is still holding the prompt
    from the interrupted attempt, _submit_once picks it back up instead of
    rendering it a second time.
    """
    with db() as conn:
        n = conn.execute(
            "UPDATE jobs SET status='queued', started_at=NULL WHERE status='running'"
        ).rowcount
    return n


async def loop():
    recovered = recover()
    if recovered:
        _state["last_error"] = f"requeued {recovered} job(s) interrupted by a restart"
    while True:
        # Reels build locally, so they drain even with the desktop asleep.
        job = await _claim(local=True)
        if job:
            kind = loads(job["params"]).get("workflow") or "reel"
            _state["current"] = job["id"]
            progress.start(job["id"], kind, stage="starting ffmpeg")
            try:
                if kind == "deliver":
                    await _run_deliver(job)
                else:
                    await _run_reel(job)
                _state["last_error"] = ""
            except Exception as e:
                _fail(job["id"], str(e))
                _state["last_error"] = str(e)
            finally:
                _state["current"] = None
                progress.done()
            continue

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
        progress.start(job["id"], loads(job["params"]).get("workflow") or "txt2img",
                       stage="sending to the node")
        try:
            await _run(job)
            _state["last_error"] = ""
        except comfy.ComfyOffline as e:
            # Not a failure. The PC went to sleep; requeue and wait it out.
            _release(job["id"], str(e))
            _state["online"] = False
            _state["last_error"] = f"node went away: {e}"
            _state["current"] = None
            progress.done()
            await asyncio.sleep(POLL_IDLE)
        except Exception as e:
            _fail(job["id"], str(e))
            _state["last_error"] = str(e)
        finally:
            _state["current"] = None
            progress.done()
