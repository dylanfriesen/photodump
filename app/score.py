"""Score a render against the style reference it was supposed to match.

Written to back-check recipe changes. Four rounds of this project were spent
arguing about whether an image "matched the style", so these are the axes the
argument kept turning on, measured instead of eyeballed:

  sat       the reference is desaturated; the failures were candy-bright
  lum       the reference is dark and moody; the failures were high-key
  contrast  hard single-source lighting spreads luminance wide
  dark      share of the frame in shadow
  blowout   the failures had harsh white specular blobs the reference lacks
  edges     thick uniform lineart marks more pixels than thin refined lineart
  detail    airbrushed gradients survive a blur; flat cel shading does not

Hue is deliberately absent. The subject's hair colour is meant to differ from
the reference's, so any palette metric would punish a correct render.

These numbers rank candidates against a common reference. They do not decide
anything on their own - always look at the images too. Moved here from
tools/compare.py so the sweep results page can use it; that CLI still works.
"""
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageChops, ImageFilter, ImageStat

W = 512  # every image is scored at one size, or edge counts track resolution

# Spread of each axis across real renders, used to put them on one scale.
SCALE = {"sat": 40, "lum": 40, "contrast": 25, "dark": 25,
         "blowout": 4, "edges": 8, "detail": 6}
AXES = list(SCALE)


def _prep(path: Path) -> Image.Image:
    with Image.open(path) as im:
        im = im.convert("RGB")
        return im.resize((W, max(1, int(W * im.height / im.width))), Image.LANCZOS)


def metrics(path: Path) -> dict:
    im = _prep(path)
    n = im.width * im.height
    sat = im.convert("HSV").getchannel("S")
    lum = im.convert("L")

    hist = lum.histogram()
    # How much detail a small blur destroys. Airbrushed gradients are already
    # smooth and barely move; hard cel edges and fine texture move a lot.
    diff = ImageStat.Stat(ImageChops.difference(lum, lum.filter(ImageFilter.GaussianBlur(2)))).mean[0]

    edges = lum.filter(ImageFilter.FIND_EDGES).histogram()
    return {
        "sat": ImageStat.Stat(sat).mean[0],
        "lum": ImageStat.Stat(lum).mean[0],
        "contrast": ImageStat.Stat(lum).stddev[0],
        "dark": 100 * sum(hist[:60]) / n,        # % shadow
        "blowout": 100 * sum(hist[245:]) / n,    # % clipped highlight
        "edges": 100 * sum(edges[40:]) / n,      # % pixels on a strong edge
        "detail": diff,                          # inverse of airbrush softness
    }


def distance(a: dict, b: dict) -> float:
    """Mean normalised deviation. 0 is identical; ~1.0 is a different look."""
    return sum(abs(a[k] - b[k]) / SCALE[k] for k in SCALE) / len(SCALE)


@lru_cache(maxsize=512)
def _cached(path: str, mtime: float) -> dict:
    return {k: round(v, 3) for k, v in metrics(Path(path)).items()}


def cached(path: Path) -> dict | None:
    """metrics(), memoised per file version. None if the file is unreadable."""
    try:
        return _cached(str(path), path.stat().st_mtime)
    except (OSError, ValueError):
        return None


def sheet(ref: Path, shots: list[Path], out: Path) -> None:
    ims = [_prep(p) for p in [ref] + shots]
    h = max(i.height for i in ims)
    canvas = Image.new("RGB", (W * len(ims), h), (20, 20, 20))
    for i, im in enumerate(ims):
        canvas.paste(im, (i * W, 0))
    canvas.save(out)
