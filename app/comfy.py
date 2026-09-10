"""Client for the ComfyUI HTTP API on the desktop render node.

Every call assumes the node may simply be asleep - that's the normal resting
state of a desktop, not an error. Callers get ComfyOffline and are expected to
leave the job queued rather than fail it.
"""
import asyncio
import contextlib
import json
import random
from pathlib import Path

import httpx
import websockets

from .config import (COMFY_HOST, COMFY_PORT, COMFY_URL, CHECKPOINT, ASPECTS,
                     VIDEO_SIZES, VIDEO_BACKEND, WAN_UNET, WAN_CLIP, WAN_VAE,
                     LTX_UNET, LTX_CLIP, LTX_VAE)

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
    except httpx.TimeoutException:
        return {"online": False, "error": "ComfyUI connection timed out. Check that ComfyUI is running and reachable over Tailscale."}
    except httpx.ConnectError:
        return {"online": False, "error": "Cannot connect to ComfyUI. Check the desktop's ComfyUI service and firewall."}
    except Exception as e:
        return {"online": False, "error": f"ComfyUI health check failed: {str(e) or type(e).__name__}"}


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


def _chain_references(wf: dict, ref_names: list[str]) -> None:
    """Feed several references into one IPAdapter by batching them.

    The graph size depends on how many references there are, so the extra
    LoadImage and ImageBatch nodes are generated rather than templated. Only
    core nodes are used for the batching itself; the IPAdapter pack supplies
    the conditioning. Per-reference weights would need IPAdapterEncoder +
    CombineEmbeds, which is a bigger change - these share one weight.
    """
    wf["10"]["inputs"]["image"] = ref_names[0]
    prev = "10"
    for i, name in enumerate(ref_names[1:], start=1):
        load, batch = f"10{i}", f"20{i}"
        wf[load] = {"class_type": "LoadImage", "inputs": {"image": name}}
        wf[batch] = {"class_type": "ImageBatch",
                     "inputs": {"image1": [prev, 0], "image2": [load, 0]}}
        prev = batch
    wf["13"]["inputs"]["image"] = [prev, 0]


def build(prompt: str, negative: str, params: dict,
          ref_names: "str | list[str] | None" = None) -> tuple[dict, int]:
    """Fill a workflow template. Returns (graph, seed) so the seed can be recorded."""
    if isinstance(ref_names, str):
        ref_names = [ref_names]
    ref_names = [r for r in (ref_names or []) if r]
    ref_name = ref_names[0] if ref_names else None

    mode = params.get("workflow")
    if not mode:
        # More than one reference cannot go through img2img, which conditions
        # on a single latent; style conditioning is the only thing that takes
        # several images at once.
        mode = ("ipadapter_multi" if len(ref_names) > 1
                else "img2img" if ref_name else "txt2img")
    if mode == "ipadapter" and len(ref_names) > 1:
        mode = "ipadapter_multi"
    if mode in ("img2img", "ipadapter", "ipadapter_multi", "outpaint", "wan_i2v") and not ref_name:
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
        if mode in ("ipadapter", "ipadapter_multi"):
            wf["13"]["inputs"]["weight"] = float(params.get("ip_weight", 0.7))
            if mode == "ipadapter_multi":
                _chain_references(wf, ref_names)
            else:
                wf["10"]["inputs"]["image"] = ref_name

    wf["3"]["inputs"] = k
    return wf, seed


# --- video backends -------------------------------------------------------
# Adding a second video model should be a table entry plus a workflow JSON,
# not new logic. Each entry says which node ids carry which role in that
# model's graph. If a model's graph cannot be described this way, write a
# sibling builder rather than contorting the table.
#
# `models` maps node id -> (input field, configured filename).
VIDEO_BACKENDS = {
    "wan": {
        "workflow": "wan_i2v",
        "models": {"20": ("unet_name", WAN_UNET),
                   "21": ("clip_name", WAN_CLIP),
                   "22": ("vae_name", WAN_VAE)},
        "image": "10",          # LoadImage
        "latent": "23",         # takes width / height / length
        "sampler": "3",
        "fps_nodes": {"24": "fps"},
        "frame_multiple": 4,    # WAN wants 4n+1 frames
        "defaults": {"steps": 20, "cfg": 5.0,
                     "sampler_name": "uni_pc", "scheduler": "simple"},
    },
    # "ltx": filled in by whoever installs LTX-2.5. Export ComfyUI's official
    # LTX-2.5 image-to-video template via Workflow -> Export (API Format),
    # save it as app/workflows/ltx_i2v.json, then describe it here. LTX uses
    # UnetLoaderGGUF / CLIPLoaderGGUF (type: ltxv) and separate video and
    # audio VAEs, so it may need more than one entry in `models` and possibly
    # a sibling builder. Do not write this from documentation - export it.
}


def _build_video(prompt: str, negative: str, params: dict, ref_name: str) -> tuple[dict, int]:
    """Image-to-video, driven by the backend table above."""
    name = params.get("video_backend") or VIDEO_BACKEND
    spec = VIDEO_BACKENDS.get(name)
    if spec is None:
        raise ComfyError(
            f"unknown video backend {name!r}; known: {sorted(VIDEO_BACKENDS)}")

    wf = _load(spec["workflow"])
    seed = params.get("seed")
    seed = int(seed) if seed and int(seed) > 0 else random.randint(1, 2**31 - 1)

    for node, (field, default) in spec["models"].items():
        wf[node]["inputs"][field] = params.get(field) or default

    wf[spec["image"]]["inputs"]["image"] = ref_name
    wf["6"]["inputs"]["text"] = prompt
    wf["7"]["inputs"]["text"] = negative

    w, h = VIDEO_SIZES.get(params.get("video_size", "story"), VIDEO_SIZES["story"])
    fps = int(params.get("fps", 16))
    length = int(round(fps * float(params.get("seconds", 3))))
    m = spec.get("frame_multiple")
    if m:
        # e.g. WAN wants 4n+1; anything else silently degrades the last chunk.
        length = max(m * 4 + 1, length - (length - 1) % m)
    wf[spec["latent"]]["inputs"].update({"width": w, "height": h, "length": length})

    k = wf[spec["sampler"]]["inputs"]
    d = spec["defaults"]
    k.update({
        "seed": seed,
        "steps": int(params.get("steps", d["steps"])),
        "cfg": float(params.get("cfg", d["cfg"])),
        "sampler_name": params.get("sampler", d["sampler_name"]),
        "scheduler": d["scheduler"],
    })
    for node, field in spec.get("fps_nodes", {}).items():
        wf[node]["inputs"][field] = float(fps)
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


# --- live progress --------------------------------------------------------
# ComfyUI publishes per-step progress only over its websocket, and only to the
# client id that submitted the prompt - a second socket listening with its own
# id hears nothing but queue counts. So the socket has to be open *before*
# submit(), with the same client id, or the opening steps are lost.
#
# This is telemetry, never control flow. If the socket will not open, or drops
# halfway, the render still completes through history polling; the UI just
# falls back to a time estimate.

WS_URL = f"ws://{COMFY_HOST}:{COMFY_PORT}/ws"

# Node class -> what to call that phase in the UI. Anything unlisted falls
# back to the class name, which is still more use than a bare node id.
NODE_STAGES = {
    "UnetLoaderGGUF": "loading model",
    "CLIPLoaderGGUF": "loading text encoder",
    "SamplerCustomAdvanced": "sampling",
    "LTXVLatentUpsampler": "upscaling latents",
    "VAEDecodeTiled": "decoding video",
    "LTXVSpatioTemporalTiledVAEDecode": "decoding video",
    "LTXVAudioVAEDecode": "decoding audio",
    "CreateVideo": "assembling video and audio",
    "SaveVideo": "saving video",
    "CheckpointLoaderSimple": "loading checkpoint",
    "UNETLoader": "loading model",
    "CLIPLoader": "loading text encoder",
    "VAELoader": "loading VAE",
    "CLIPTextEncode": "encoding prompt",
    "KSampler": "sampling",
    "KSamplerAdvanced": "sampling",
    "VAEDecode": "decoding",
    "VAEEncode": "encoding image",
    "SaveImage": "saving",
    "SaveWEBM": "encoding video",
    "SaveAnimatedWEBP": "encoding video",
    "LoadImage": "reading reference",
    "ImagePadForOutpaint": "padding canvas",
}


def stage_for(graph: dict, node_id) -> str:
    """Human name for whichever node ComfyUI says it is executing."""
    cls = (graph.get(str(node_id)) or {}).get("class_type", "")
    return NODE_STAGES.get(cls, cls.lower() or "rendering")


async def _pump(ws, handler):
    async for msg in ws:
        if isinstance(msg, (bytes, bytearray)):
            continue          # preview frames; we do not surface these
        try:
            packet = json.loads(msg)
        except ValueError:
            continue
        try:
            handler(packet.get("type", ""), packet.get("data") or {})
        except Exception:
            pass              # a bad telemetry frame must not kill the render


@contextlib.asynccontextmanager
async def progress_socket(client_id: str, handler):
    """Stream this client's execution events to `handler(type, data)`.

    Yields immediately whether or not the socket opened, so callers can wrap
    a submit-and-poll block in it unconditionally.
    """
    ws = None
    task = None
    try:
        ws = await asyncio.wait_for(
            websockets.connect(f"{WS_URL}?clientId={client_id}", max_size=None),
            timeout=10,
        )
        task = asyncio.create_task(_pump(ws, handler))
    except Exception:
        ws = task = None
    try:
        yield
    finally:
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if ws:
            with contextlib.suppress(Exception):
                await ws.close()


async def pending_ids() -> set[str]:
    """Prompt ids the node is running or has queued.

    A requeue after the desktop slept must not blindly resubmit: the node may
    still be holding - or already running - the prompt from the last attempt,
    and a second submit renders the same job twice.
    """
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{COMFY_URL}/queue")
            r.raise_for_status()
            body = r.json()
    except Exception as e:
        raise ComfyOffline(str(e)) from e
    ids = set()
    for key in ("queue_running", "queue_pending"):
        for item in body.get(key) or []:
            # Entries are [number, prompt_id, graph, extra, outputs].
            if len(item) > 1 and isinstance(item[1], str):
                ids.add(item[1])
    return ids


async def pending_prompt(prompt_id: str) -> dict | None:
    """Recover the submitted graph and telemetry client, including desktop jobs."""
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{COMFY_URL}/queue")
            r.raise_for_status()
        for key in ("queue_running", "queue_pending"):
            for item in r.json().get(key) or []:
                if len(item) >= 4 and item[1] == prompt_id:
                    return {"graph": item[2], "client_id": item[3].get("client_id"),
                            "created": item[3].get("create_time")}
        return None
    except Exception as e:
        raise ComfyOffline(str(e)) from e


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
