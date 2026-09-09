"""Source-image prep for the extend/outpaint task.

SDXL falls apart well above ~1 megapixel, and a 16GB card will happily try to
allocate its way into an OOM if handed a 1920x2404 canvas. So the source is
scaled down first so that the *padded* result lands in the model's comfort
zone; upscaling back up afterwards is a separate, cheap problem.
"""
from pathlib import Path

from PIL import Image, ImageOps

from .config import outpaint_pads

TARGET_MP = 1.3  # final canvas megapixels; SDXL is trained around 1.0


def prepare(src: Path, dst: Path, target: str, anchor: str = "center",
            target_mp: float = TARGET_MP) -> dict:
    """Scale `src` so that padding it to `target` ratio lands near target_mp.

    Returns the pads to apply to the written image at `dst`.
    """
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        w, h = im.size

        # Pads at native size tell us how big the final canvas *would* be.
        p = outpaint_pads(w, h, target, anchor)
        fw, fh = w + p["left"] + p["right"], h + p["top"] + p["bottom"]

        scale = min(1.0, ((target_mp * 1_000_000) / (fw * fh)) ** 0.5)
        if scale < 1.0:
            w, h = max(8, int(w * scale)), max(8, int(h * scale))
            im = im.resize((w, h), Image.LANCZOS)

        # Recompute on the real, scaled dimensions so the /8 rounding is honest.
        p = outpaint_pads(w, h, target, anchor)
        im.save(dst, "PNG")

    p["source_size"] = [w, h]
    p["final_size"] = [w + p["left"] + p["right"], h + p["top"] + p["bottom"]]
    return p
