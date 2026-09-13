"""Score a render against the style reference it was supposed to match.

Written to back-check recipe changes. Four rounds of this project were spent
arguing about whether an image "matched the style", so these are the axes the
argument kept turning on, measured instead of eyeballed:

  saturation   the reference is desaturated; the failures were candy-bright
  luminance    the reference is dark and moody; the failures were high-key
  contrast     hard single-source lighting spreads luminance wide
  blowout      the failures had harsh white specular blobs the reference lacks
  edges        thick uniform lineart marks more pixels than thin refined lineart
  softness     airbrushed gradients survive a blur; flat cel shading does not

Hue is deliberately absent. The subject's hair colour is meant to differ from
the reference's, so any palette metric would punish a correct render.

These numbers rank candidates against a common reference. They do not decide
anything on their own - always look at the contact sheet too.
"""
import sys
from pathlib import Path

from PIL import Image, ImageFilter, ImageStat

W = 512  # every image is scored at one size, or edge counts track resolution


def _prep(path: Path) -> Image.Image:
    im = Image.open(path).convert("RGB")
    return im.resize((W, int(W * im.height / im.width)), Image.LANCZOS)


def metrics(path: Path) -> dict:
    im = _prep(path)
    n = im.width * im.height
    sat = im.convert("HSV").getchannel("S")
    lum = im.convert("L")

    hist = lum.histogram()
    blur = lum.filter(ImageFilter.GaussianBlur(2))
    # How much detail a small blur destroys. Airbrushed gradients are already
    # smooth and barely move; hard cel edges and fine texture move a lot.
    diff = ImageStat.Stat(
        Image.frombytes("L", im.size,
                        bytes(abs(a - b) for a, b in zip(lum.tobytes(), blur.tobytes())))
    ).mean[0]

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


# Spread of each axis across real renders, used to put them on one scale.
SCALE = {"sat": 40, "lum": 40, "contrast": 25, "dark": 25,
         "blowout": 4, "edges": 8, "detail": 6}


def distance(a: dict, b: dict) -> float:
    """Mean normalised deviation. 0 is identical; ~1.0 is a different look."""
    return sum(abs(a[k] - b[k]) / SCALE[k] for k in SCALE) / len(SCALE)


def sheet(ref: Path, shots: list[Path], out: Path) -> None:
    ims = [_prep(p) for p in [ref] + shots]
    h = max(i.height for i in ims)
    canvas = Image.new("RGB", (W * len(ims), h), (20, 20, 20))
    for i, im in enumerate(ims):
        canvas.paste(im, (i * W, 0))
    canvas.save(out)


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        print("usage: compare.py REFERENCE CANDIDATE [CANDIDATE ...]")
        return 2
    ref, shots = Path(argv[1]), [Path(p) for p in argv[2:]]
    rm = metrics(ref)
    cols = list(SCALE)
    print(f"{'image':<34}" + "".join(f"{c:>10}" for c in cols) + f"{'dist':>8}")
    print(f"{ref.name[:33]:<34}" + "".join(f"{rm[c]:>10.1f}" for c in cols) + f"{'--':>8}")
    rows = []
    for p in shots:
        m = metrics(p)
        rows.append((distance(rm, m), p, m))
    for d, p, m in rows:
        print(f"{p.name[:33]:<34}" + "".join(f"{m[c]:>10.1f}" for c in cols) + f"{d:>8.3f}")
    print("\nclosest to the reference's look, best first:")
    for d, p, _ in sorted(rows):
        print(f"  {d:.3f}  {p.name}")
    out = Path("/tmp/compare_sheet.png")
    sheet(ref, shots, out)
    print(f"\ncontact sheet (reference first): {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
