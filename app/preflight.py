"""Check every workflow against what the render node actually has installed.

The five graphs in app/workflows were written from documentation, not captured
from a running ComfyUI, so the first real session is expected to surface
mismatches. Without this the symptom is an opaque failed job; with it you get
the node class or model filename that is missing, per workflow.
"""
from . import comfy
from .config import CHECKPOINT, WAN_CLIP, WAN_UNET, WAN_VAE

# Which loader node carries which configured filename, and where /object_info
# publishes the list of files that loader can actually see.
LOADERS = {
    "CheckpointLoaderSimple": "ckpt_name",
    "UNETLoader": "unet_name",
    "CLIPLoader": "clip_name",
    "VAELoader": "vae_name",
}

WORKFLOWS = [
    ("txt2img",   "Fuse",            {"workflow": "txt2img"}),
    ("img2img",   "Fuse w/ reference", {"workflow": "img2img"}),
    ("ipadapter", "IP-Adapter",      {"workflow": "ipadapter"}),
    ("ipadapter_multi", "Multi-reference", {"workflow": "ipadapter_multi"}),
    ("outpaint",  "Extend",          {"workflow": "outpaint"}),
    ("wan_i2v",   "Animate",         {"workflow": "wan_i2v"}),
]


def _options(info: dict, node: str, field: str) -> list[str] | None:
    """The file list a loader offers, or None if the node type is absent."""
    try:
        return list(info[node]["input"]["required"][field][0])
    except (KeyError, IndexError, TypeError):
        return None


async def run() -> dict:
    info = await comfy.object_info()

    results = []
    for name, label, params in WORKFLOWS:
        # Dummy references keep ref-consuming graphs on their real path. The
        # multi-reference graph only grows its ImageBatch chain when given more
        # than one, so hand it two - otherwise those nodes go unvalidated.
        # txt2img takes none: handing it one made auto-resolution pick img2img,
        # so that entry silently validated the wrong graph. The multi-reference
        # graph needs two, or its ImageBatch chain is never built.
        refs = {"txt2img": [], "ipadapter_multi": ["preflight.png", "preflight2.png"]}.get(
            name, ["preflight.png"])
        graph, _ = comfy.build("preflight", "", dict(params), refs)
        classes = {n["class_type"] for n in graph.values()}
        missing_nodes = sorted(c for c in classes if c not in info)

        missing_models = []
        for node in graph.values():
            field = LOADERS.get(node["class_type"])
            if not field:
                continue
            want = node["inputs"].get(field)
            have = _options(info, node["class_type"], field)
            if have is None or not want:
                continue          # node itself already reported missing
            if want not in have:
                missing_models.append({"loader": node["class_type"], "field": field,
                                       "want": want, "count": len(have)})

        results.append({
            "workflow": name, "label": label,
            "ok": not missing_nodes and not missing_models,
            "missing_nodes": missing_nodes,
            "missing_models": missing_models,
        })

    ready = sum(1 for r in results if r["ok"])
    return {
        "online": True,
        "node_count": len(info),
        "workflows": results,
        "ready": ready,
        "total": len(results),
        "ok": ready == len(results),
        "configured": {"checkpoint": CHECKPOINT, "wan_unet": WAN_UNET,
                       "wan_clip": WAN_CLIP, "wan_vae": WAN_VAE},
    }
