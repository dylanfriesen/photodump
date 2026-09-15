"""Backups, fixed seeds and sweeps: the work that is staged without a GPU."""
import json
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

from app import backup, db as database, main, score


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for sub in ("refs", "out"):
            (self.root / sub).mkdir()
        for target, attr, value in (
            (database, "DB_PATH", self.root / "photodump.db"),
            (backup, "DB_PATH", self.root / "photodump.db"),
            (backup, "REFS", self.root / "refs"),
            (backup, "OUT", self.root / "out"),
            (main, "REFS", self.root / "refs"),
            (main, "OUT", self.root / "out"),
        ):
            p = patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        database.init()
        # The worker's wake() only sets an event; nothing renders here.
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)

    def img(self, folder, name, colour):
        Image.new("RGB", (64, 80), colour).save(self.root / folder / name)


class Backups(Base):
    def test_snapshot_is_consistent_and_links_files(self):
        self.img("refs", "r.png", (10, 10, 10))
        self.img("out", "o.png", (200, 10, 10))
        with database.db() as conn:
            conn.execute("INSERT INTO jobs(prompt) VALUES('x')")
        info = backup.snapshot(date(2026, 9, 15), root=self.root / "backups")
        self.assertEqual((info["jobs"], info["refs_linked"], info["out_linked"]), (1, 1, 1))
        snap = Path(info["dir"])
        with sqlite3.connect(snap / "photodump.db") as c:
            self.assertEqual(c.execute("SELECT prompt FROM jobs").fetchone()[0], "x")
        # A hardlink: the backup survives the original being deleted.
        (self.root / "out" / "o.png").unlink()
        self.assertTrue((snap / "out" / "o.png").exists())
        self.assertFalse((snap / "photodump.db.partial").exists())

    def test_rerun_same_day_links_nothing_twice(self):
        self.img("out", "o.png", (1, 2, 3))
        backup.snapshot(date(2026, 9, 15), root=self.root / "backups")
        again = backup.snapshot(date(2026, 9, 15), root=self.root / "backups")
        self.assertEqual(again["out_linked"], 0)

    def test_prune_keeps_the_window_and_ignores_strangers(self):
        root = self.root / "backups"
        for name in ("2026-08-01", "2026-09-01", "2026-09-02", "2026-09-15", "keep-me"):
            (root / name).mkdir(parents=True)
        (root / "backup.log").write_text("log")
        gone = backup.prune(keep=14, root=root, today=date(2026, 9, 15))
        self.assertEqual(gone, ["2026-08-01", "2026-09-01"])
        self.assertTrue((root / "2026-09-02").exists())
        self.assertTrue((root / "keep-me").exists())
        self.assertTrue((root / "backup.log").exists())


class Seeds(Base):
    def test_fixed_seed_is_stored_and_offset_across_a_batch(self):
        r = self.client.post("/api/generate", json={"prompt": "x", "seed": 100, "count": 3})
        self.assertEqual(r.status_code, 200)
        with database.db() as conn:
            seeds = [json.loads(p)["seed"] for (p,) in conn.execute("SELECT params FROM jobs ORDER BY id")]
        self.assertEqual(seeds, [100, 101, 102])

    def test_no_seed_means_random(self):
        self.client.post("/api/generate", json={"prompt": "x"})
        with database.db() as conn:
            (p,) = conn.execute("SELECT params FROM jobs").fetchone()
        self.assertIsNone(json.loads(p)["seed"])


SPEC = {
    "name": "grid-1",
    "seeds": [11, 22],
    "base": {"prompt": "a cat", "workflow": "img2img", "ref_ids": [1], "denoise": 0.45,
             "second_pass": True, "second_pass_denoise": 0.65},
    "cells": [
        {"label": "plain", "set": {"hires": False}},
        {"label": "pixel", "set": {"hires": True, "hires_method": "pixel", "hires_denoise": 0.35}},
    ],
}


class Sweeps(Base):
    def test_grid_is_cells_by_seeds_in_one_batch(self):
        r = self.client.post("/api/sweeps", json=SPEC)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(r.json()["queued"]), 4)
        with database.db() as conn:
            rows = [dict(x) for x in conn.execute("SELECT params, batch_id FROM jobs ORDER BY id")]
        self.assertEqual(len({x["batch_id"] for x in rows}), 1)
        params = [json.loads(x["params"]) for x in rows]
        self.assertEqual([(p["sweep"]["cell"], p["seed"]) for p in params],
                         [("plain", 11), ("plain", 22), ("pixel", 11), ("pixel", 22)])
        self.assertEqual(params[2]["hires_denoise"], 0.35)
        self.assertTrue(all(p["second_pass"] for p in params))

    def test_invalid_cell_writes_nothing(self):
        bad = json.loads(json.dumps(SPEC))
        bad["cells"][1]["set"]["hires_method"] = "nearest"
        self.assertEqual(self.client.post("/api/sweeps", json=bad).status_code, 400)
        with database.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)

    def test_names_are_unique_and_validated(self):
        self.assertEqual(self.client.post("/api/sweeps", json=SPEC).status_code, 200)
        self.assertEqual(self.client.post("/api/sweeps", json=SPEC).status_code, 409)
        for name in ("", "Has Caps", "../x"):
            self.assertEqual(self.client.post("/api/sweeps", json={**SPEC, "name": name}).status_code, 400)
        self.assertEqual(self.client.post(
            "/api/sweeps", json={**SPEC, "name": "dupe", "cells": [SPEC["cells"][0]] * 2}).status_code, 400)

    def test_hold_applies_to_every_job(self):
        r = self.client.post("/api/sweeps", json={**SPEC, "not_before": "2099-01-01T10:00Z"})
        self.assertEqual(r.status_code, 200)
        with database.db() as conn:
            holds = {x for (x,) in conn.execute("SELECT not_before FROM jobs")}
        self.assertEqual(holds, {"2099-01-01 10:00:00"})

    def test_results_pair_passes_and_score_against_the_reference(self):
        self.img("refs", "ref.png", (20, 20, 30))
        with database.db() as conn:
            conn.execute("INSERT INTO refs(id, filename, label) VALUES (1, 'ref.png', 'dark')")
        self.client.post("/api/sweeps", json=SPEC)
        # Land job 1 (pass 1) and chain its pass 2 the way the worker does.
        self.img("out", "1_11_0.png", (30, 30, 40))
        self.img("out", "5_11_0.png", (240, 240, 240))
        with database.db() as conn:
            conn.execute("UPDATE jobs SET status='done' WHERE id=1")
            conn.execute("INSERT INTO images(id, job_id, filename, seed) VALUES (1, 1, '1_11_0.png', 11)")
            from app import worker
            with patch.object(worker, "wake"):
                job = dict(conn.execute("SELECT * FROM jobs WHERE id=1").fetchone())
                worker._queue_second_pass(conn, job, json.loads(job["params"]))
            conn.execute("UPDATE jobs SET status='done' WHERE id=5")
            conn.execute("INSERT INTO images(id, job_id, filename, seed) VALUES (2, 5, '5_11_0.png', 11)")

        body = self.client.get("/api/sweeps/grid-1").json()
        self.assertTrue(body["chained"])
        self.assertEqual(body["reference"]["id"], 1)
        plain = body["cells"][0]
        self.assertEqual(plain["label"], "plain")
        self.assertEqual(len(plain["runs"]), 2)
        first = plain["runs"][0]
        self.assertEqual(first["pass1"]["id"], 1)
        self.assertEqual(first["pass2"]["id"], 5)
        near = first["pass1"]["images"][0]["distance"]
        far = first["pass2"]["images"][0]["distance"]
        self.assertLess(near, far)    # the dark render is closer to the dark reference
        self.assertIsNone(plain["runs"][1]["pass2"])

        listing = self.client.get("/api/sweeps").json()
        self.assertEqual((listing[0]["name"], listing[0]["jobs"], listing[0]["done"]), ("grid-1", 5, 2))
        self.assertEqual(self.client.get("/api/sweeps/nope").status_code, 404)


class Score(unittest.TestCase):
    def test_matches_the_original_pixel_loop(self):
        # score.py replaced a per-pixel Python loop with ImageChops.difference;
        # the numbers must not have moved, or old notes stop being comparable.
        from PIL import ImageFilter, ImageStat
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "grad.png"
            im = Image.linear_gradient("L").resize((256, 320)).convert("RGB")
            im.save(p)
            lum = score._prep(p).convert("L")
            blur = lum.filter(ImageFilter.GaussianBlur(2))
            old = ImageStat.Stat(Image.frombytes(
                "L", lum.size, bytes(abs(a - b) for a, b in zip(lum.tobytes(), blur.tobytes())))).mean[0]
            self.assertAlmostEqual(score.metrics(p)["detail"], old, places=6)


if __name__ == "__main__":
    unittest.main()
