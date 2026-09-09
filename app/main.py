import asyncio
import json
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import captions, comfy, preflight, reels, worker
from .config import ASPECTS, CHECKPOINT, OUT, REFS, THUMBS
from .db import db, init, loads, rows
from .prompts import (MODES, STARTERS, compile_negative, compile_parts,
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


@app.post("/api/generate")
async def api_generate(payload: dict):
    count = max(1, min(int(payload.get("count", 1)), 8))
    prompt = payload.get("prompt") or compile_prompt(
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
        "workflow": payload.get("workflow"),
        "checkpoint": payload.get("checkpoint"),
        "feathering": int(payload.get("feathering", 40)),
        "extend_target": payload.get("extend_target", "portrait"),
        "extend_anchor": payload.get("extend_anchor", "center"),
        "video_size": payload.get("video_size", "story"),
        "seconds": float(payload.get("seconds", 3)),
        "fps": int(payload.get("fps", 16)),
    }
    ref_id = payload.get("ref_id") or None

    ids = []
    with db() as conn:
        for _ in range(count):
            cur = conn.execute(
                "INSERT INTO jobs (recipe_id, prompt, negative, params, ref_id, src_image_id) "
                "VALUES (?,?,?,?,?,?)",
                (payload.get("recipe_id"), prompt, negative, json.dumps(params), ref_id,
                 payload.get("src_image_id")),
            )
            ids.append(cur.lastrowid)
    return {"queued": ids, "node": worker.status()}


# ---------- reels ----------

@app.post("/api/reels")
async def api_reel(payload: dict):
    """Queue a reel. Renders locally, so it works with the desktop asleep."""
    ids = payload.get("image_ids") or []
    if len(ids) < 2:
        raise HTTPException(400, "pick at least two stills")
    params = {
        "workflow": "reel",
        "image_ids": [int(i) for i in ids],
        "bpm": float(payload["bpm"]) if payload.get("bpm") else None,
        "beats_per_shot": int(payload.get("beats_per_shot", 4)),
        "seconds": float(payload.get("seconds", 2.0)),
        "motion": payload.get("motion", "kenburns"),
        "transition": payload.get("transition", "cut"),
        "audio_ref_id": payload.get("audio_ref_id"),
    }
    shot = reels.shot_seconds(params["bpm"], params["beats_per_shot"], params["seconds"])
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO jobs (prompt, negative, params) VALUES (?,?,?)",
            (f"reel from {len(ids)} stills", "", json.dumps(params)),
        )
    return {"queued": [cur.lastrowid], "shot_seconds": round(shot, 3),
            "total_seconds": round(reels.total_seconds(shot, len(ids), params["transition"]), 2)}


# ---------- queue + gallery ----------

@app.get("/api/jobs")
async def api_jobs(limit: int = 40):
    with db() as conn:
        return rows(conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)))


@app.delete("/api/jobs/{job_id}")
async def api_cancel(job_id: int):
    with db() as conn:
        conn.execute("UPDATE jobs SET status='cancelled' WHERE id=? AND status='queued'", (job_id,))
    return {"ok": True}


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
async def api_images(limit: int = 120, favourites: bool = False):
    q = (
        "SELECT i.*, j.prompt, j.params FROM images i "
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
    result = await captions.draft(
        payload.get("subject_a", ""),
        payload.get("subject_b", ""),
        payload.get("mode", "design_fusion"),
        payload.get("extra", ""),
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
