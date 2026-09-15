"""Score renders against a style reference from the command line.

The metrics live in app/score.py (the sweep results page uses them too); see
its docstring for what each axis measures. Run with the repo root on the path:

  docker run --rm -v $PWD:/r -w /r photodump-photodump \
    python tools/compare.py data/refs/REF.png data/out/A.png data/out/B.png
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.score import SCALE, distance, metrics, sheet  # noqa: E402


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
