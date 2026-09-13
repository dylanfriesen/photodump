"""Source-image prep that runs on kanto before anything reaches the node.

SDXL falls apart well above ~1 megapixel, and a 16GB card will happily try to
allocate its way into an OOM if handed a 1920x2404 canvas. So the source is
scaled down first so that the *padded* result lands in the model's comfort
zone; upscaling back up afterwards is a separate, cheap problem.
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageMath, ImageOps

from .config import outpaint_pads

TARGET_MP = 1.3  # final canvas megapixels; SDXL is trained around 1.0
# Ceiling for an upscale tail's second pass. txt2img hires already runs
# 832x1216 x1.5 = 2.28MP on the 16GB card, so that is the proven size.
HIRES_MAX_MP = 2.3
# The fill holds ~10 full-size buffers, several of them float. 16MP bounds one
# fill near a gigabyte; no reference that large could be img2img'd anyway.
FILL_MAX_MP = 16


class TooLarge(ValueError):
    """Source image exceeds what a kanto-side fill is allowed to allocate."""


def hires_scale(src: Path, requested: float, max_mp: float = HIRES_MAX_MP) -> float:
    """The largest scale <= `requested` whose output stays under `max_mp`.

    img2img keeps its reference's size, so a large reference times 1.5 can
    ask the card for 4MP+ - the graph cannot see pixel dimensions, so the cap
    has to be applied here, where the file is.
    """
    with Image.open(src) as im:
        w, h = im.size
    cap = ((max_mp * 1_000_000) / (w * h)) ** 0.5
    return round(max(1.0, min(requested, cap)), 3)


def fill_regions(src: Path, dst: Path, regions: list) -> list:
    """Paint over rectangles with colour pulled in from their surroundings.

    For lettering and logos in a style reference: img2img at 0.45 copies
    whatever is in the source, so text there becomes garbled fake text in the
    output, and negatives cannot remove it. With the region already smooth
    colour, there is nothing to copy.

    The fill is normalised convolution: blur only the pixels *outside* the
    boxes and divide by how much known area each blur saw, so the text itself
    never leaks into its replacement (a plain blur-in-place smears it). Coarse
    radii cover the middle of a big box, finer ones overwrite wherever they
    have enough support, so the edges match their surroundings closely. No
    numpy or OpenCV in the image, hence ImageMath. It does not invent texture;
    the sampler does that.

    `regions` are [x, y, w, h] fractions of the image, so a box drawn on a
    thumbnail means the same thing at full size. Returns the pixel boxes used.
    """
    with Image.open(src) as im:
        # The header is read without decoding, so the check costs nothing.
        if im.width * im.height > FILL_MAX_MP * 1_000_000:
            raise TooLarge(f"{im.width}x{im.height} is over the {FILL_MAX_MP}MP fill limit")
        im = ImageOps.exif_transpose(im).convert("RGB")
    W, H = im.size
    mask = Image.new("L", (W, H), 0)
    draw = ImageDraw.Draw(mask)
    boxes = []
    for r in regions or []:
        try:
            x, y, w, h = (min(1.0, max(0.0, float(v))) for v in r)
        except (TypeError, ValueError):
            continue
        box = (int(x * W), int(y * H), int(min(1.0, x + w) * W), int(min(1.0, y + h) * H))
        if box[2] - box[0] < 2 or box[3] - box[1] < 2:
            continue
        draw.rectangle(box, fill=255)
        boxes.append(list(box))
    if not boxes:
        im.save(dst, "PNG")
        return []

    # Grow the boxes a few pixels: letter edges are anti-aliased past where a
    # box drawn by eye stops, and a leftover fringe fills as grey haze.
    mask = mask.filter(ImageFilter.MaxFilter(7))
    known = ImageOps.invert(mask)
    premult = Image.composite(im, Image.new("RGB", im.size), known)
    canvas = None
    for radius in (max(W, H) // 4, 96, 48, 24, 12, 6, 3):
        support = known.filter(ImageFilter.GaussianBlur(radius))
        k = support.convert("F")
        layer = Image.merge("RGB", [
            ImageMath.lambda_eval(lambda a: a["c"] * 255 / (a["k"] + 0.01),
                                  c=ch.convert("F"), k=k).convert("L")
            for ch in premult.filter(ImageFilter.GaussianBlur(radius)).split()])
        if canvas is None:
            canvas = layer        # widest radius: defined almost everywhere
            continue
        # Trust a finer level only where it saw enough real pixels; ramp in
        # softly so level boundaries do not show as rings.
        canvas = Image.composite(layer, canvas, support.point(lambda v: min(255, max(0, (v - 20) * 6))))
    out = Image.composite(im, canvas, known)
    # Soften the seam and any level banding inside the boxes only.
    out = Image.composite(out, out.filter(ImageFilter.GaussianBlur(2)), known)
    out.save(dst, "PNG")
    return boxes


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
