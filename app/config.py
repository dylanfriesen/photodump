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
DB_PATH = DATA / "photodump.db"

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


# --- video (WAN 2.2 TI2V-5B) -------------------------------------------
# The 5B variant is single-model and fits 16GB comfortably; the 14B needs
# dual high/low-noise models and GGUF quantisation to be worth attempting.
#
# NOTE: the stock WAN workflows ship an fp8_e4m3fn text encoder, which is
# exactly the format broken on RDNA4/Windows. Use the fp16 or GGUF encoder.
WAN_UNET = os.getenv("WAN_UNET", "wan2.2_ti2v_5B_fp16.safetensors")
WAN_CLIP = os.getenv("WAN_CLIP", "umt5_xxl_fp16.safetensors")
WAN_VAE = os.getenv("WAN_VAE", "wan2.2_vae.safetensors")

# Which video model the Animate task uses. See VIDEO_BACKENDS in comfy.py.
VIDEO_BACKEND = os.getenv("VIDEO_BACKEND", "wan")

# LTX-2.5 model files, unset until the desktop session installs them.
LTX_UNET = os.getenv("LTX_UNET", "LTX25-distilled-DiT-Q4_K_M.gguf")
LTX_CLIP = os.getenv("LTX_CLIP", "gemma4-12b-with-proj-ltx-2.5-Q5_K_M.gguf")
LTX_VAE = os.getenv("LTX_VAE", "ltx-2.5-video-vae-bf16.safetensors")

# Output buckets for the Animate task. The 540-class entries exist because a
# 16GB card running a GGUF quant is far happier there than at 720p, and an
# upscale pass afterwards is cheap.
VIDEO_SIZES = {
    "story": (704, 1280),      # 9:16 reels
    "story_540": (544, 960),   # 9:16, the size AMD reports as workable
    "portrait": (704, 896),    # 4:5 feed
    "portrait_540": (544, 680),
    "square": (960, 960),
    "square_540": (640, 640),
}
