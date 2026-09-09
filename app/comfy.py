"""Client for the ComfyUI HTTP API on the desktop render node.

Every call assumes the node may simply be asleep - that's the normal resting
state of a desktop, not an error. Callers get ComfyOffline and are expected to
leave the job queued rather than fail it.
"""
import json
import random
from pathlib import Path

import httpx

from .config import (COMFY_URL, CHECKPOINT, ASPECTS, VIDEO_SIZES,
                     WAN_UNET, WAN_CLIP, WAN_VAE)

WORKFLOWS = Path(__file__).parent / "workflows"


class ComfyOffline(Exception):
    """Render node unreachable. Expected and recoverable - never fail a job on this."""


class ComfyError(Exception):
    """Node answered but rejected the work. This one is a real failure."""


def _load(name: str) -> dict:
    return json.loads((WORKFLOWS / f"{name}.json").read_text())


async def health() -> dict:
    """Probe the node. Short timeout: an asleep box should not stall the UI."""
    try:
        async with httpx.AsyncClient(timeout=4) as c:
            r = await c.get(f"{COMFY_URL}/system_stats")
            r.raise_for_status()
            return {"online": True, "stats": r.json()}
    except Exception as e:
        return {"online": False, "error": str(e)}


async def object_info() -> dict:
    """The node's full capability map. Raises ComfyOffline if unreachable."""
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(f"{COMFY_URL}/object_info")
            r.raise_for_status()
            return r.json()
    except Exception as e:
        raise ComfyOffline(str(e)) from e


async def available() -> dict:
    """Checkpoints the node actually has, and whether IP-Adapter nodes exist."""
    info = await object_info()

    ckpts = []
    node = info.get("CheckpointLoaderSimple", {})
    try:
        ckpts = node["input"]["required"]["ckpt_name"][0]
    except (KeyError, IndexError, TypeError):
        pass

    return {
        "checkpoints": ckpts,
        "has_ipadapter": "IPAdapterUnifiedLoader" in info,
        "node_count": len(info),
    }


async def upload_image(path: Path) -> str:
    """Push a reference image to the node; returns the name to use in LoadImage."""
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            with path.open("rb") as fh:
                r = await c.post(
                    f"{COMFY_URL}/upload/image",
                    files={"image": (path.name, fh, "image/png")},
                    data={"overwrite": "true"},
                )
            r.raise_for_status()
            body = r.json()
    except httpx.HTTPStatusError as e:
        raise ComfyError(f"upload rejected: {e}") from e
    except Exception as e:
        raise ComfyOffline(str(e)) from e

    name = body.get("name", path.name)
    sub = body.get("subfolder") or ""
    return f"{sub}/{name}" if sub else name


def build(prompt: str, negative: str, params: dict, ref_name: str | None = None) -> tuple[dict, int]:
    """Fill a workflow template. Returns (graph, seed) so the seed can be recorded."""
    mode = params.get("workflow") or ("img2img" if ref_name else "txt2img")
    if mode in ("img2img", "ipadapter", "outpaint", "wan_i2v") and not ref_name:
        mode = "txt2img"

    if mode == "wan_i2v":
        return _build_video(prompt, negative, params, ref_name)

    wf = _load(mode)
    seed = params.get("seed")
    if not seed or int(seed) <= 0:
        seed = random.randint(1, 2**31 - 1)
    seed = int(seed)

    ckpt = params.get("checkpoint") or CHECKPOINT
    wf["4"]["inputs"]["ckpt_name"] = ckpt
    wf["6"]["inputs"]["text"] = prompt
    wf["7"]["inputs"]["text"] = negative

    k = wf["3"]["inputs"]
    k["seed"] = seed
    k["steps"] = int(params.get("steps", 30))
    k["cfg"] = float(params.get("cfg", 5.0))
    k["sampler_name"] = params.get("sampler", "euler_ancestral")

    if mode == "img2img":
        k["denoise"] = float(params.get("denoise", 0.65))
        wf["10"]["inputs"]["image"] = ref_name
    elif mode == "outpaint":
        wf["10"]["inputs"]["image"] = ref_name
        pads = params.get("pads") or {}
        for side in ("left", "right", "top", "bottom"):
            wf["14"]["inputs"][side] = int(pads.get(side, 0))
        wf["14"]["inputs"]["feathering"] = int(params.get("feathering", 40))
    else:
        w, h = ASPECTS.get(params.get("aspect", "portrait"), ASPECTS["portrait"])
        wf["5"]["inputs"]["width"] = w
        wf["5"]["inputs"]["height"] = h
        if mode == "ipadapter":
            wf["10"]["inputs"]["image"] = ref_name
            wf["13"]["inputs"]["weight"] = float(params.get("ip_weight", 0.7))

    wf["3"]["inputs"] = k
    return wf, seed


def _build_video(prompt: str, negative: str, params: dict, ref_name: str) -> tuple[dict, int]:
    """WAN 2.2 image-to-video. Separate builder: it shares no nodes with SDXL."""
    wf = _load("wan_i2v")
    seed = params.get("seed")
    seed = int(seed) if seed and int(seed) > 0 else random.randint(1, 2**31 - 1)

    wf["20"]["inputs"]["unet_name"] = params.get("wan_unet") or WAN_UNET
    wf["21"]["inputs"]["clip_name"] = params.get("wan_clip") or WAN_CLIP
    wf["22"]["inputs"]["vae_name"] = params.get("wan_vae") or WAN_VAE
    wf["10"]["inputs"]["image"] = ref_name
    wf["6"]["inputs"]["text"] = prompt
    wf["7"]["inputs"]["text"] = negative

    w, h = VIDEO_SIZES.get(params.get("video_size", "story"), VIDEO_SIZES["story"])
    fps = int(params.get("fps", 16))
    seconds = float(params.get("seconds", 3))
    # WAN wants 4n+1 frames; anything else silently degrades the last chunk.
    length = int(round(fps * seconds))
    length = max(17, length - (length - 1) % 4)

    wf["23"]["inputs"].update({"width": w, "height": h, "length": length})
    wf["3"]["inputs"].update({
        "seed": seed,
        "steps": int(params.get("steps", 20)),
        "cfg": float(params.get("cfg", 5.0)),
        "sampler_name": params.get("sampler", "uni_pc"),
        "scheduler": "simple",
    })
    wf["24"]["inputs"]["fps"] = float(fps)
    return wf, seed


async def submit(graph: dict, client_id: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{COMFY_URL}/prompt", json={"prompt": graph, "client_id": client_id})
    except Exception as e:
        raise ComfyOffline(str(e)) from e

    if r.status_code >= 400:
        # A 400 here is a malformed graph or a missing checkpoint - our fault,
        # not the node's. Surface the node's own error text; it is specific.
        raise ComfyError(f"HTTP {r.status_code}: {r.text[:600]}")
    return r.json()["prompt_id"]


async def history(prompt_id: str) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{COMFY_URL}/history/{prompt_id}")
            r.raise_for_status()
            return r.json().get(prompt_id)
    except Exception as e:
        raise ComfyOffline(str(e)) from e


async def fetch(filename: str, subfolder: str, ftype: str) -> bytes:
    try:
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.get(
                f"{COMFY_URL}/view",
                params={"filename": filename, "subfolder": subfolder, "type": ftype},
            )
            r.raise_for_status()
            return r.content
    except Exception as e:
        raise ComfyOffline(str(e)) from e
