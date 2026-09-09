import os
from pathlib import Path

COMFY_HOST = os.getenv("COMFY_HOST", "100.109.223.93")
COMFY_PORT = int(os.getenv("COMFY_PORT", "8188"))
COMFY_URL = f"http://{COMFY_HOST}:{COMFY_PORT}"

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://100.109.223.93:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:14b-instruct")

CHECKPOINT = os.getenv("CHECKPOINT", "waiNSFWIllustrious_v140.safetensors")

DATA = Path(os.getenv("DATA_DIR", "/srv/data"))
REFS = DATA / "refs"
OUT = DATA / "out"
THUMBS = DATA / "thumbs"
DB_PATH = DATA / "forge.db"

for _d in (REFS, OUT, THUMBS):
    _d.mkdir(parents=True, exist_ok=True)

# Instagram-native canvases. SDXL wants ~1M pixels; these are the
# standard SDXL buckets closest to each IG ratio.
ASPECTS = {
    "portrait": (832, 1216),   # 4:5  - the IG feed default, most screen real estate
    "square":   (1024, 1024),  # 1:1
    "story":    (768, 1344),   # 9:16 - stories / reels covers
    "landscape": (1216, 832),  # 3:2-ish
}


# Target aspect ratios for the extend/outpaint task, as width/height.
EXTEND_RATIOS = {
    "portrait": 4 / 5,
    "square": 1.0,
    "story": 9 / 16,
    "landscape": 3 / 2,
}


def outpaint_pads(w: int, h: int, target: str, anchor: str = "center") -> dict:
    """Pixels to add on each side to reach `target` ratio without shrinking the source.

    Only ever grows the canvas - the original pixels are never cropped, which is
    the whole point: nothing the user shot gets thrown away.
    """
    ratio = EXTEND_RATIOS.get(target, 4 / 5)
    cur = w / h
    left = top = right = bottom = 0

    if cur > ratio:          # too wide -> grow vertically
        need = int(round(w / ratio)) - h
        if anchor == "top":
            bottom = need
        elif anchor == "bottom":
            top = need
        else:
            top, bottom = need // 2, need - need // 2
    elif cur < ratio:        # too tall -> grow horizontally
        need = int(round(h * ratio)) - w
        if anchor == "left":
            right = need
        elif anchor == "right":
            left = need
        else:
            left, right = need // 2, need - need // 2

    # SDXL VAE works in multiples of 8; round pads up so the latent grid is clean.
    def up8(base, pad):
        return pad + (-(base + pad) % 8)

    return {
        "left": up8(w, left) if left else left,
        "right": up8(w + left, right) if right else right,
        "top": up8(h, top) if top else top,
        "bottom": up8(h + top, bottom) if bottom else bottom,
    }
