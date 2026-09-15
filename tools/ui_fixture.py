"""Seed a throwaway data dir for tools/ui_check.py. Never point this at live data.

Synthetic images only. Gives every screen something to render: three
references, a finished style-match pair, a Fuse still, a queued job to hold,
and a half-finished sweep.
"""
import json
import os
import sys
from pathlib import Path

DATA = Path(os.environ["DATA_DIR"])
if DATA.resolve() == Path("/srv/data").resolve() or (DATA / "photodump.db").exists():
    sys.exit(f"refusing to seed {DATA}: it is not an empty scratch directory")

from PIL import Image, ImageDraw  # noqa: E402

from app import db  # noqa: E402  (imports config, which creates the subdirs)
from app.prompts import compile_negative  # noqa: E402


def picture(path: Path, colour, size=(680, 856), label=""):
    im = Image.new("RGB", size, colour)
    d = ImageDraw.Draw(im)
    d.ellipse((size[0] * .25, size[1] * .2, size[0] * .75, size[1] * .6), fill=tuple(255 - c for c in colour))
    d.text((20, 20), label, fill=(255, 255, 255))
    im.save(path)


db.init()
for i, colour in enumerate([(40, 30, 70), (20, 90, 80), (120, 70, 20)], start=1):
    picture(DATA / "refs" / f"ref{i}.png", colour, label=f"ref {i}")
for n, colour in [(1, (60, 40, 110)), (2, (200, 180, 120)), (3, (30, 60, 140)),
                  (4, (70, 50, 100)), (5, (210, 190, 140))]:
    picture(DATA / "out" / f"img{n}.png", colour, label=f"image {n}")

create = {"aspect": "portrait", "steps": 40, "cfg": 5.0, "denoise": 0.45, "ip_weight": 0.7,
          "workflow": "img2img", "free_prompt": True, "second_pass": True,
          "second_pass_denoise": 0.65, "hires": False, "clean_regions": [[0.1, 0.8, 0.6, 0.1]],
          "recipe": {"subject_a": "", "subject_b": "", "mode": "design_fusion", "extra": ""}}
pass2 = {k: v for k, v in create.items() if k not in ("second_pass", "clean_regions")} | {"denoise": 0.65}
fuse = {"aspect": "portrait", "steps": 30, "cfg": 5.0, "workflow": None, "free_prompt": False,
        "recipe": {"subject_a": "gardevoir", "subject_b": "gojo satoru", "mode": "design_fusion", "extra": ""}}
negative = compile_negative("text, watermark")
sweep = lambda cell: {"name": "ui-fixture", "cell": cell, "set": {"hires": cell == "pixel"}}  # noqa: E731

with db.db() as conn:
    for r in (1, 2, 3):
        conn.execute("INSERT INTO refs (id, filename, label, kind) VALUES (?,?,?, 'style')",
                     (r, f"ref{r}.png", f"ref {r}"))
    job = ("INSERT INTO jobs (id, prompt, negative, params, ref_id, ref_ids, src_image_id, batch_id, status, "
           "finished_at) VALUES (?,?,?,?,?,?,?,?,?, datetime('now'))")
    conn.execute(job, (1, "A quiet illustrated portrait", negative, json.dumps(create), 1, "[1]", None, "pair", "done"))
    conn.execute(job, (2, "A quiet illustrated portrait", negative, json.dumps(pass2), None, "", 1, "pair", "done"))
    conn.execute(job, (3, "gardevoir x gojo", negative, json.dumps(fuse), None, "", None, "fuse", "done"))
    conn.execute(job, (4, "a queued idea", negative, json.dumps(create | {"second_pass": False}), 1, "[1]",
                       None, "later", "queued"))
    # A sweep: cell "plain" finished both passes, cell "pixel" is still waiting.
    conn.execute(job, (5, "sweep", negative, json.dumps(create | {"seed": 7, "sweep": sweep("plain")}), 1, "[1]",
                       None, "sweep", "done"))
    conn.execute(job, (6, "sweep", negative, json.dumps(pass2 | {"seed": 7, "sweep": sweep("plain")}), None, "",
                       4, "sweep", "done"))
    conn.execute(job, (7, "sweep", negative, json.dumps(create | {"seed": 7, "sweep": sweep("pixel")}), 1, "[1]",
                       None, "sweep", "queued"))
    for image_id, job_id, name in [(1, 1, "img1.png"), (2, 2, "img2.png"), (3, 3, "img3.png"),
                                   (4, 5, "img4.png"), (5, 6, "img5.png")]:
        conn.execute("INSERT INTO images (id, job_id, filename, seed) VALUES (?,?,?,7)", (image_id, job_id, name))
    # Queue left running on purpose: the check instance points COMFY_HOST at
    # an unroutable address, so node jobs stay queued while the reel and
    # carousel the check submits still build locally, as they do in life.
    db.set_setting(conn, "queue_paused", "")

print(f"seeded {DATA}")
