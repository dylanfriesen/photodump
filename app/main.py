import asyncio
import json
import shutil
import tempfile
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import captions, comfy, deliver, imageops, preflight, reels, worker
from .config import ASPECTS, CHECKPOINT, OUT, REFS, THUMBS
from .db import db, init, loads, rows, set_setting
from .prompts import (MODES, QUALITY, STARTERS, compile_negative, compile_parts,
                      compile_prompt)

WEB = Path(__file__).parent.parent / "web"
ALLOWED = {".png", ".jpg", ".jpeg", ".webp"}
AUDIO = {".mp3", ".m4a", ".wav", ".ogg", ".aac", ".flac"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    init()
    task = asyncio.create_task(worker.loop())
    yield
    task.cancel()


app = FastAPI(title="photodump", lifespan=lifespan)


# ---------- meta ----------

@app.get("/api/status")
async def api_status():
    return worker.status()


@app.get("/api/config")
async def api_config():
    return {
        "modes": MODES,
        "starters": STARTERS,
        "aspects": list(ASPECTS.keys()),
        "checkpoint": CHECKPOINT,
        # So the Create task can show exactly what it will prepend.
        "quality": QUALITY,
        "negative": compile_negative(),
    }


@app.get("/api/node")
async def api_node():
    """What the render node actually has installed. Drives UI affordances."""
    try:
        return await comfy.available()
    except comfy.ComfyOffline as e:
        return JSONResponse({"offline": True, "error": str(e)}, status_code=503)


@app.get("/api/preflight")
async def api_preflight():
    """What each workflow needs vs what the node has. See app/preflight.py."""
    try:
        return await preflight.run()
    except comfy.ComfyOffline as e:
        return JSONResponse({"online": False, "error": str(e)}, status_code=503)


# ---------- references ----------

@app.get("/api/refs")
async def api_refs():
    with db() as conn:
        return rows(conn.execute("SELECT * FROM refs ORDER BY id DESC"))


@app.post("/api/refs")
async def api_add_ref(
    file: UploadFile = File(...),
    label: str = Form(""),
    kind: str = Form("character"),
    notes: str = Form(""),
):
    ext = Path(file.filename or "").suffix.lower()
    allowed = AUDIO if kind == "audio" else ALLOWED
    if ext not in allowed:
        raise HTTPException(400, f"unsupported type {ext!r} for kind {kind!r}")
    name = f"{uuid.uuid4().hex}{ext}"
    with (REFS / name).open("wb") as out:
        shutil.copyfileobj(file.file, out)
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO refs (filename, label, kind, notes) VALUES (?,?,?,?)",
            (name, label or Path(file.filename or name).stem, kind, notes),
        )
        return {"id": cur.lastrowid, "filename": name}


@app.post("/api/refs/{ref_id}/clean-preview")
async def api_clean_preview(ref_id: int, payload: dict):
    """The reference exactly as img2img will see it after filling `regions`.

    Runs the same fill the worker runs, on kanto, so a bad box can be seen
    and redrawn before it costs a render.
    """
    with db() as conn:
        row = conn.execute("SELECT * FROM refs WHERE id=?", (ref_id,)).fetchone()
    if not row or row["kind"] == "audio":
        raise HTTPException(404, "no such image reference")
    regions = _regions(payload.get("regions"))
    dst = Path(tempfile.gettempdir()) / f"photodump_clean_{uuid.uuid4().hex}.png"
    try:
        async with _fill_lock:
            await asyncio.to_thread(imageops.fill_regions, REFS / row["filename"], dst, regions)
        data = dst.read_bytes()
    except imageops.TooLarge as e:
        raise HTTPException(413, str(e))
    except OSError as e:          # PIL raises UnidentifiedImageError, an OSError
        raise HTTPException(422, f"cannot read reference: {e}")
    finally:
        dst.unlink(missing_ok=True)
    return Response(data, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.delete("/api/refs/{ref_id}")
async def api_del_ref(ref_id: int):
    with db() as conn:
        row = conn.execute("SELECT * FROM refs WHERE id=?", (ref_id,)).fetchone()
        if not row:
            raise HTTPException(404, "no such reference")
        conn.execute("DELETE FROM refs WHERE id=?", (ref_id,))
    (REFS / row["filename"]).unlink(missing_ok=True)
    return {"ok": True}


# ---------- prompting ----------

@app.post("/api/preview")
async def api_preview(payload: dict):
    """Compile without queueing, so the tag string is inspectable before spending a render."""
    a = payload.get("subject_a", "")
    b = payload.get("subject_b", "")
    mode = payload.get("mode", "design_fusion")
    extra = payload.get("extra", "")
    prompt = compile_prompt(a, b, mode, extra)
    return {
        "prompt": prompt,
        "negative": compile_negative(payload.get("negative", "")),
        "groups": compile_parts(a, b, mode, extra),
        "tokens": len([t for t in prompt.split(",") if t.strip()]),
    }


PUBLIC_WORKFLOWS = {None, "", "txt2img", "img2img", "ipadapter", "ipadapter_multi",
                    "outpaint", "wan_i2v"}

# One fill at a time: each holds several full-size float buffers, and nothing
# else stops a burst of previews on large references eating the box's memory.
_fill_lock = asyncio.Semaphore(1)


def _regions(raw) -> list:
    """Keep only well-formed [x, y, w, h] fraction boxes; drop the rest."""
    out = []
    for r in raw or []:
        try:
            x, y, w, h = (float(v) for v in r)
        except (TypeError, ValueError):
            continue
        if 0 <= x < 1 and 0 <= y < 1 and w > 0 and h > 0:
            out.append([round(x, 4), round(y, 4), round(min(w, 1 - x), 4), round(min(h, 1 - y), 4)])
    return out[:32]


@app.post("/api/generate")
async def api_generate(payload: dict):
    count = max(1, min(int(payload.get("count", 1)), 8))
    # A free-form prompt wins outright. Only the Fuse task compiles one from
    # subject_a/subject_b - everything else says what it wants directly.
    free = (payload.get("prompt") or "").strip()
    if free and payload.get("quality", True) and not QUALITY.split(",")[0] in free:
        free = f"{QUALITY}, {free}"
    prompt = free or compile_prompt(
        payload.get("subject_a", ""),
        payload.get("subject_b", ""),
        payload.get("mode", "design_fusion"),
        payload.get("extra", ""),
    )
    negative = payload.get("negative_full") or compile_negative(payload.get("negative", ""))

    params = {
        "aspect": payload.get("aspect", "portrait"),
        "steps": int(payload.get("steps", 30)),
        "cfg": float(payload.get("cfg", 5.0)),
        "denoise": float(payload.get("denoise", 0.65)),
        "ip_weight": float(payload.get("ip_weight", 0.7)),
        "ip_weight_type": payload.get("ip_weight_type"),
        "workflow": payload.get("workflow"),
        "checkpoint": payload.get("checkpoint"),
        "free_prompt": bool(free),
        "feathering": int(payload.get("feathering", 40)),
        "extend_target": payload.get("extend_target", "portrait"),
        "extend_anchor": payload.get("extend_anchor", "center"),
        "video_size": payload.get("video_size", "story"),
        "video_backend": payload.get("video_backend"),   # wan | ltx
        # Style match: pass 1 copies a reference's rendering technique at low
        # denoise, pass 2 re-renders from that output at higher denoise to fix
        # the colours pass 1 inherits along with the style. See _queue_second_pass.
        "second_pass": bool(payload.get("second_pass")),
        "second_pass_denoise": float(payload.get("second_pass_denoise", 0.65)),
        # Tags only pass 2 gets, appended to the shared prompt / negative.
        "second_pass_prompt_add": str(payload.get("second_pass_prompt_add") or "").strip(),
        "second_pass_negative_add": str(payload.get("second_pass_negative_add") or "").strip(),
        "hires": bool(payload.get("hires")),
        "hires_scale": float(payload.get("hires_scale", 1.5)),
        "hires_steps": int(payload.get("hires_steps", 20)),
        # None, not a number: build() picks a different default per upscale
        # route, and a value baked in here would silently override it.
        "hires_denoise": (float(payload["hires_denoise"])
                          if payload.get("hires_denoise") is not None else None),
        # img2img only: "pixel" (lanczos, default) or "latent" (bicubic).
        "hires_method": payload.get("hires_method") or "pixel",
        # [x, y, w, h] fractions of the reference to fill before img2img.
        "clean_regions": _regions(payload.get("clean_regions")),
        "seconds": float(payload.get("seconds", 3)),
        "fps": int(payload.get("fps", 16)),
        # Kept so a caption can be drafted from what this image actually is,
        # months later, rather than from whatever the form happens to say.
        "recipe": {
            "subject_a": payload.get("subject_a", ""),
            "subject_b": payload.get("subject_b", ""),
            "mode": payload.get("mode", "design_fusion"),
            "extra": payload.get("extra", ""),
        },
    }
    ref_id = payload.get("ref_id") or None
    # Several references may be attached, in the order the user picked them.
    ref_ids = [int(r) for r in (payload.get("ref_ids") or []) if r]
    if not ref_ids and ref_id:
        ref_ids = [int(ref_id)]
    # The UI stops this, but the API is reachable directly and img2img loads
    # only the first image - accepting the rest would silently discard them.
    if len(ref_ids) > 1 and params["workflow"] == "img2img":
        raise HTTPException(
            400, "img2img conditions on a single image; pass one reference "
                 "or use ipadapter_multi")
    # Job 68 asked for an LTX story_hd clip with no workflow. build() resolved
    # its lone reference to img2img and rendered a 680x856 still in 11s, which
    # was read as a suspiciously fast video render. Refuse instead of guessing.
    if ((payload.get("video_backend") or payload.get("video_size"))
            and params["workflow"] != "wan_i2v"):
        raise HTTPException(
            400, "video_backend/video_size need workflow 'wan_i2v'; without it "
                 "this would render a still")
    # The *_hires graphs are chosen by build() from `hires`; naming one directly
    # skipped the worker's prep (cleanup, pixel cap) and pass-1 suppression.
    if params["workflow"] not in PUBLIC_WORKFLOWS:
        raise HTTPException(
            400, f"workflow must be one of {sorted(w for w in PUBLIC_WORKFLOWS if w)}; "
                 "upscaling is `hires: true`, not a workflow name")
    if params["hires_method"] not in ("pixel", "latent"):
        raise HTTPException(400, "hires_method must be 'pixel' or 'latent'")

    ids = []
    # count=N creates N single-image jobs. They share a batch id so the mailer
    # can send the whole request as one email rather than N of them.
    batch = uuid.uuid4().hex
    with db() as conn:
        for _ in range(count):
            cur = conn.execute(
                "INSERT INTO jobs (recipe_id, prompt, negative, params, ref_id, ref_ids, "
                "src_image_id, batch_id) VALUES (?,?,?,?,?,?,?,?)",
                (payload.get("recipe_id"), prompt, negative, json.dumps(params),
                 ref_ids[0] if ref_ids else None, json.dumps(ref_ids),
                 payload.get("src_image_id"), batch),
            )
            ids.append(cur.lastrowid)
    worker.wake()
    return {"queued": ids, "node": worker.status()}


# ---------- reels ----------

@app.post("/api/reels")
async def api_reel(payload: dict):
    """Queue a reel. Renders locally, so it works with the desktop asleep."""
    shots = payload.get("shots")
    if shots:
        shots = [{"src": "ref" if s.get("src") == "ref" else "image", "id": int(s["id"])}
                 for s in shots]
    else:
        shots = [{"src": "image", "id": int(i)} for i in (payload.get("image_ids") or [])]
    if len(shots) < 2:
        raise HTTPException(400, "pick at least two stills")
    params = {
        "workflow": "reel",
        "shots": shots,
        "bpm": float(payload["bpm"]) if payload.get("bpm") else None,
        "beats_per_shot": int(payload.get("beats_per_shot", 4)),
        "seconds": float(payload.get("seconds", 2.0)),
        "motion": payload.get("motion", "kenburns"),
        "transition": payload.get("transition", "cut"),
        "audio_ref_id": payload.get("audio_ref_id"),
    }
    ids = shots
    shot = reels.shot_seconds(params["bpm"], params["beats_per_shot"], params["seconds"])
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO jobs (prompt, negative, params) VALUES (?,?,?)",
            (f"reel from {len(ids)} stills", "", json.dumps(params)),
        )
    worker.wake()
    return {"queued": [cur.lastrowid], "shot_seconds": round(shot, 3),
            "total_seconds": round(reels.total_seconds(shot, len(ids), params["transition"]), 2)}


@app.post("/api/images/{image_id}/deliver")
async def api_deliver(image_id: int, payload: dict):
    """Queue an Instagram-ready version of a clip, reel or still.

    Clips become an H.264 mp4; stills become a 1080-wide JPEG, cropped when
    they are within a few percent of the ratio and padded otherwise.
    """
    with db() as conn:
        row = conn.execute("SELECT * FROM images WHERE id=?", (image_id,)).fetchone()
    if not row:
        raise HTTPException(404, "no such image")
    still = Path(row["filename"]).suffix.lower() in ALLOWED

    target = payload.get("target", "feed" if still else "reel")
    if target not in deliver.TARGETS:
        raise HTTPException(400, f"target must be one of {sorted(deliver.TARGETS)}")
    params = {"workflow": "deliver", "src_image_id": image_id, "target": target}
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO jobs (prompt, negative, params) VALUES (?,?,?)",
            (f"instagram {target} encode of #{image_id}", "", json.dumps(params)))
    worker.wake()
    return {"queued": [cur.lastrowid], "target": target,
            "size": deliver.TARGETS[target], "still": still}


# ---------- queue + gallery ----------

@app.get("/api/jobs")
async def api_jobs(limit: int = Query(40, ge=1, le=500)):
    # src_job_id links a style-match pass 2 (and an animate clip) to the job
    # whose output it started from, so the queue can show the pair as one
    # request. out_filename is that job's latest image, for a thumbnail.
    with db() as conn:
        return rows(conn.execute(
            "SELECT j.*, i.job_id AS src_job_id, "
            "(SELECT o.filename FROM images o WHERE o.job_id = j.id "
            " ORDER BY o.id DESC LIMIT 1) AS out_filename "
            "FROM jobs j LEFT JOIN images i ON i.id = j.src_image_id "
            "ORDER BY j.id DESC LIMIT ?", (limit,)))


@app.delete("/api/jobs/{job_id}")
async def api_cancel(job_id: int):
    with db() as conn:
        conn.execute("UPDATE jobs SET status='cancelled' WHERE id=? AND status='queued'", (job_id,))
    return {"ok": True}


@app.post("/api/jobs/{job_id}/pause")
async def api_pause(job_id: int):
    """Pause a job. Running jobs stop at the node; queued ones just park.

    A running job is not paused here - `control` is a request the worker picks
    up on its next poll. The worker owns `status` for whatever it is
    rendering, and writing that status from here would race it.
    """
    with db() as conn:
        row = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise HTTPException(404, "no such job")
        if row["status"] == "running":
            conn.execute("UPDATE jobs SET control='pause' WHERE id=?", (job_id,))
            return {"ok": True, "pending": True}
        if row["status"] == "queued":
            conn.execute("UPDATE jobs SET status='paused' WHERE id=?", (job_id,))
            return {"ok": True, "pending": False}
        raise HTTPException(400, f"cannot pause a {row['status']} job")


@app.post("/api/jobs/{job_id}/stop")
async def api_stop(job_id: int):
    """Cancel a job outright. Terminal - resume will not bring it back."""
    with db() as conn:
        row = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise HTTPException(404, "no such job")
        if row["status"] == "running":
            conn.execute("UPDATE jobs SET control='stop' WHERE id=?", (job_id,))
            return {"ok": True, "pending": True}
        if row["status"] in ("queued", "paused"):
            conn.execute(
                "UPDATE jobs SET status='cancelled', finished_at=datetime('now'), "
                "prompt_id='', resume_latent='', resume_step=0 WHERE id=?",
                (job_id,),
            )
            return {"ok": True, "pending": False}
        raise HTTPException(400, f"cannot stop a {row['status']} job")


@app.post("/api/jobs/{job_id}/resume")
async def api_resume(job_id: int):
    """Return a paused job to the queue.

    `attempts` is left alone: pause already gave back the attempt it consumed,
    so resuming does not need to reset the ceiling the way requeue does.
    Clearing not_before makes resume mean "now" even for a job that was
    scheduled - otherwise resuming a scheduled job appears to do nothing.
    """
    with db() as conn:
        row = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise HTTPException(404, "no such job")
        if row["status"] != "paused":
            raise HTTPException(400, f"cannot resume a {row['status']} job")
        conn.execute(
            "UPDATE jobs SET status='queued', not_before=NULL, error='' WHERE id=?",
            (job_id,),
        )
    worker.wake()
    return {"ok": True}


@app.post("/api/jobs/{job_id}/schedule")
async def api_schedule(job_id: int, payload: dict):
    """Hold a job until a UTC timestamp. `not_before: null` clears the hold.

    Stored as a string compared in SQL against datetime('now'), so it must be
    'YYYY-MM-DD HH:MM:SS' in UTC like every other timestamp in this schema. A
    local-time value here silently runs the job at the wrong hour, so it is
    parsed and normalised rather than trusted.
    """
    raw = payload.get("not_before")
    when = None
    if raw not in (None, ""):
        text = str(raw).strip().replace("T", " ").replace("Z", "")
        try:
            when = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            try:
                when = datetime.strptime(text[:16], "%Y-%m-%d %H:%M")
            except ValueError:
                raise HTTPException(400, "not_before must be UTC 'YYYY-MM-DD HH:MM[:SS]'")
    with db() as conn:
        row = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise HTTPException(404, "no such job")
        if row["status"] not in ("queued", "paused"):
            raise HTTPException(400, f"cannot schedule a {row['status']} job")
        conn.execute(
            "UPDATE jobs SET not_before=?, status='queued' WHERE id=?",
            (when.strftime("%Y-%m-%d %H:%M:%S") if when else None, job_id),
        )
    return {"ok": True, "not_before": when.strftime("%Y-%m-%d %H:%M:%S") if when else None}


@app.post("/api/queue/pause")
async def api_queue_pause():
    """Stop dispatching new jobs. Whatever is rendering runs to completion."""
    with db() as conn:
        set_setting(conn, "queue_paused", "1")
    return {"ok": True, "queue_paused": True}


@app.post("/api/queue/resume")
async def api_queue_resume():
    with db() as conn:
        set_setting(conn, "queue_paused", "")
    return {"ok": True, "queue_paused": False}


@app.post("/api/jobs/{job_id}/requeue")
async def api_requeue(job_id: int):
    """Put a failed or cancelled job back on the queue with a fresh attempt count.

    A job that burned through MAX_ATTEMPTS against a broken graph is otherwise
    dead; after the graph is fixed you want to retry it, not retype it.
    """
    with db() as conn:
        row = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise HTTPException(404, "no such job")
        if row["status"] not in ("failed", "cancelled"):
            raise HTTPException(400, f"cannot requeue a {row['status']} job")
        conn.execute(
            "UPDATE jobs SET status='queued', attempts=0, error='', "
            "started_at=NULL, finished_at=NULL WHERE id=?",
            (job_id,),
        )
    return {"ok": True}


@app.get("/api/images")
async def api_images(limit: int = Query(120, ge=1, le=500), favourites: bool = False):
    q = (
        "SELECT i.*, j.prompt, j.negative, j.params, j.ref_ids, j.ref_id, j.src_image_id FROM images i "
        "JOIN jobs j ON j.id = i.job_id "
        f"{'WHERE i.favourite=1 ' if favourites else ''}"
        "ORDER BY i.id DESC LIMIT ?"
    )
    with db() as conn:
        return rows(conn.execute(q, (limit,)))


@app.post("/api/images/{image_id}/favourite")
async def api_fav(image_id: int):
    with db() as conn:
        conn.execute("UPDATE images SET favourite = 1 - favourite WHERE id=?", (image_id,))
        row = conn.execute("SELECT favourite FROM images WHERE id=?", (image_id,)).fetchone()
    if not row:
        raise HTTPException(404, "no such image")
    return {"favourite": row["favourite"]}


@app.delete("/api/images/{image_id}")
async def api_del_image(image_id: int):
    with db() as conn:
        row = conn.execute("SELECT * FROM images WHERE id=?", (image_id,)).fetchone()
        if not row:
            raise HTTPException(404, "no such image")
        conn.execute("DELETE FROM images WHERE id=?", (image_id,))
    (OUT / row["filename"]).unlink(missing_ok=True)
    (THUMBS / f"{row['filename']}.jpg").unlink(missing_ok=True)
    return {"ok": True}


@app.post("/api/images/{image_id}/caption")
async def api_caption(image_id: int, payload: dict):
    """Draft a caption for this image, from the recipe it was rendered with.

    The client used to send the studio form, which meant captioning an old
    render described whatever was typed in the panel at that moment. The
    image's own job is the only honest source.
    """
    with db() as conn:
        row = conn.execute(
            "SELECT j.prompt, j.params FROM images i JOIN jobs j ON j.id = i.job_id "
            "WHERE i.id=?", (image_id,)).fetchone()
    if not row:
        raise HTTPException(404, "no such image")
    recipe = (loads(row["params"]) or {}).get("recipe") or {}

    result = await captions.draft(
        recipe.get("subject_a", ""),
        recipe.get("subject_b", ""),
        recipe.get("mode", "design_fusion"),
        recipe.get("extra", ""),
        raw_prompt=row["prompt"],
    )
    if not result.get("ok"):
        return JSONResponse(result, status_code=503)
    with db() as conn:
        conn.execute(
            "UPDATE images SET caption=?, hashtags=? WHERE id=?",
            (result["caption"], " ".join(result["hashtags"]), image_id),
        )
    return result


# ---------- recipes ----------

@app.get("/api/recipes")
async def api_recipes():
    with db() as conn:
        return rows(conn.execute("SELECT * FROM recipes ORDER BY id DESC"))


@app.post("/api/recipes")
async def api_save_recipe(payload: dict):
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO recipes (name, subject_a, subject_b, mode, extra, negative, params) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                payload.get("name") or "untitled",
                payload.get("subject_a", ""),
                payload.get("subject_b", ""),
                payload.get("mode", "design_fusion"),
                payload.get("extra", ""),
                payload.get("negative", ""),
                json.dumps(payload.get("params", {})),
            ),
        )
        return {"id": cur.lastrowid}


@app.delete("/api/recipes/{recipe_id}")
async def api_del_recipe(recipe_id: int):
    with db() as conn:
        conn.execute("DELETE FROM recipes WHERE id=?", (recipe_id,))
    return {"ok": True}


# ---------- files ----------

def _serve(base: Path, name: str):
    p = (base / name).resolve()
    if not str(p).startswith(str(base.resolve())) or not p.is_file():
        raise HTTPException(404, "not found")
    return FileResponse(p)


@app.get("/refs/{name}")
async def serve_ref(name: str):
    return _serve(REFS, name)


@app.get("/out/{name}")
async def serve_out(name: str):
    return _serve(OUT, name)


@app.get("/thumbs/{name}")
async def serve_thumb(name: str):
    p = (THUMBS / name).resolve()
    if p.is_file():
        return FileResponse(p)
    return _serve(OUT, name.removesuffix(".jpg"))


app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
