"""Still-image paths that need no GPU: graph shape, kanto-side prep, delivery.

Rendered quality is not testable here - these pin down what reaches the node
and what kanto does to files on either side of it.
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw, ImageStat

from app import comfy, deliver, imageops, main, preflight, worker
from app.db import SCHEMA


def ref(name="ref.png"):
    return name


class UpscaleTailGraphs(unittest.TestCase):
    def build(self, **params):
        graph, _ = comfy.build("p", "n", {"workflow": "img2img", "denoise": 0.45, **params}, ref())
        return graph

    def test_plain_img2img_is_unchanged(self):
        g = self.build()
        self.assertNotIn("31", g)
        self.assertEqual(g["8"]["inputs"]["samples"], ["3", 0])

    def test_pixel_route_is_the_default(self):
        g = self.build(hires=True, hires_scale=1.5, hires_denoise=None)
        self.assertEqual(g["33"]["class_type"], "ImageScaleBy")
        self.assertEqual(g["33"]["inputs"]["scale_by"], 1.5)
        self.assertEqual(g["34"]["inputs"]["pixels"], ["33", 0])
        self.assertEqual(g["31"]["inputs"]["latent_image"], ["34", 0])
        self.assertEqual(g["31"]["inputs"]["denoise"], 0.35)
        self.assertEqual(g["8"]["inputs"]["samples"], ["31", 0])
        self.assertEqual(g["3"]["inputs"]["denoise"], 0.45)   # pass 1 untouched

    def test_latent_route(self):
        g = self.build(hires=True, hires_method="latent", hires_denoise=None)
        self.assertEqual(g["30"]["class_type"], "LatentUpscaleBy")
        self.assertEqual(g["31"]["inputs"]["latent_image"], ["30", 0])
        self.assertEqual(g["31"]["inputs"]["denoise"], 0.45)
        self.assertNotIn("33", g)

    def test_explicit_denoise_wins_even_when_zero_like(self):
        g = self.build(hires=True, hires_denoise=0.2, hires_steps=12)
        self.assertEqual(g["31"]["inputs"]["denoise"], 0.2)
        self.assertEqual(g["31"]["inputs"]["steps"], 12)
        self.assertEqual(g["31"]["inputs"]["seed"], g["3"]["inputs"]["seed"])

    def test_style_match_pass_one_does_not_upscale(self):
        # Pass 2 re-renders the frame at 0.65; upscaling pass 1 is wasted work.
        g = self.build(hires=True, second_pass=True)
        self.assertNotIn("31", g)

    def test_txt2img_hires_default_survives_none(self):
        g, _ = comfy.build("p", "n", {"workflow": "txt2img", "hires": True, "hires_denoise": None})
        self.assertEqual(g["31"]["inputs"]["denoise"], 0.45)

    def test_saved_through_node_9(self):
        # The mock, and the worker's output collection, key stills on node 9.
        for method in ("pixel", "latent"):
            g = self.build(hires=True, hires_method=method)
            self.assertEqual(g["9"]["inputs"]["images"], ["8", 0])

    def test_every_input_reference_exists(self):
        for method in ("pixel", "latent"):
            g = self.build(hires=True, hires_method=method)
            for nid, node in g.items():
                for v in node["inputs"].values():
                    if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str):
                        self.assertIn(v[0], g, f"node {nid} -> missing {v[0]}")

    def test_preflight_covers_both_routes(self):
        names = {w[0] for w in preflight.WORKFLOWS}
        self.assertTrue({"img2img_hires", "img2img_hires_pixel"} <= names)

    def test_upscale_nodes_have_stage_names(self):
        for cls in ("LatentUpscaleBy", "ImageScaleBy"):
            self.assertEqual(comfy.NODE_STAGES[cls], "upscaling")


class KantoPrep(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def img(self, w, h, colour=(180, 30, 30)):
        p = self.dir / f"src_{w}x{h}.png"
        Image.new("RGB", (w, h), colour).save(p)
        return p

    def test_hires_scale_keeps_small_sources(self):
        self.assertEqual(imageops.hires_scale(self.img(680, 856), 1.5), 1.5)

    def test_hires_scale_caps_large_sources(self):
        s = imageops.hires_scale(self.img(1200, 1500), 1.5)
        self.assertGreater(s, 1.0)
        self.assertLess(s, 1.5)
        self.assertLessEqual(1200 * s * 1500 * s, imageops.HIRES_MAX_MP * 1_000_000 * 1.01)

    def test_hires_scale_never_shrinks(self):
        self.assertEqual(imageops.hires_scale(self.img(3000, 3000), 1.5), 1.0)

    def test_fill_removes_text_and_keeps_the_rest(self):
        src = self.img(400, 500)
        with Image.open(src) as im:
            d = ImageDraw.Draw(im)
            d.rectangle((40, 60, 120, 400), fill=(250, 250, 250))   # "lettering"
            im.save(src)
        dst = self.dir / "clean.png"
        boxes = imageops.fill_regions(src, dst, [[30 / 400, 50 / 500, 100 / 400, 360 / 500]])
        self.assertEqual(boxes, [[30, 50, 130, 410]])
        with Image.open(dst) as out:
            inside = ImageStat.Stat(out.crop((40, 60, 120, 400))).mean
            outside = out.getpixel((300, 250))
        # The white must not leak into its own replacement.
        self.assertTrue(all(abs(a - b) < 12 for a, b in zip(inside, (180, 30, 30))), inside)
        self.assertEqual(outside, (180, 30, 30))

    def test_fill_ignores_garbage_and_slivers(self):
        src = self.img(100, 100)
        dst = self.dir / "c.png"
        self.assertEqual(imageops.fill_regions(src, dst, [["x", 0, 1, 1], [0.5, 0.5, 0.001, 0.2]]), [])
        self.assertTrue(dst.exists())

    def test_fill_refuses_huge_sources_before_decoding(self):
        src = self.dir / "huge.png"
        Image.new("L", (5000, 4000)).save(src)
        with self.assertRaises(imageops.TooLarge):
            imageops.fill_regions(src, self.dir / "x.png", [[0, 0, 0.5, 0.5]])

    def test_api_region_sanitiser(self):
        got = main._regions([[0.9, 0.9, 0.5, 0.5], [1.2, 0, 0.1, 0.1], "nope", [0, 0, 0, 0.1]])
        self.assertEqual(got, [[0.9, 0.9, 0.1, 0.1]])


class Img2imgPrep(unittest.TestCase):
    """worker._prep_img2img, with the database write stubbed out."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.writes = []
        orig_db, orig_data = worker.db, worker.DATA
        test = self

        class FakeConn:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, args):
                test.writes.append(json.loads(args[0]))

        worker.db = lambda: FakeConn()
        worker.DATA = self.dir
        self.addCleanup(lambda: setattr(worker, "db", orig_db))
        self.addCleanup(lambda: setattr(worker, "DATA", orig_data))

    def src(self, w, h):
        p = self.dir / "s.png"
        Image.new("RGB", (w, h), (1, 2, 3)).save(p)
        return p

    def test_oversized_source_skips_the_tail(self):
        params = {"hires": True, "hires_scale": 1.5}
        worker._prep_img2img({"id": 9}, params, self.src(2000, 2000))
        self.assertFalse(params["hires"])
        self.assertEqual(self.writes[-1]["hires_scale_requested"], 1.5)
        g, _ = comfy.build("p", "n", {"workflow": "img2img", **params}, "x.png")
        self.assertNotIn("31", g)

    def test_normal_source_writes_nothing(self):
        params = {"hires": True, "hires_scale": 1.5}
        out = worker._prep_img2img({"id": 9}, params, self.src(680, 856))
        self.assertEqual(self.writes, [])
        self.assertTrue(params["hires"])
        self.assertEqual(out.name, "s.png")

    def test_clean_regions_swap_the_source_and_record_boxes(self):
        params = {"clean_regions": [[0, 0, 0.5, 0.5]]}
        out = worker._prep_img2img({"id": 9}, params, self.src(100, 100))
        self.assertEqual(out.name, ".clean_9.png")
        self.assertEqual(self.writes[-1]["clean_boxes"], [[0, 0, 50, 50]])


class GenerateGuards(unittest.TestCase):
    """Refusals happen before any database write, so no schema is needed."""

    def post(self, body):
        from fastapi.testclient import TestClient
        return TestClient(main.app).post("/api/generate", json=body)

    def test_internal_graph_names_are_refused(self):
        # Naming the graph skipped the worker's prep and pass-1 suppression.
        for wf in ("img2img_hires_pixel", "img2img_hires", "txt2img_hires"):
            r = self.post({"prompt": "x", "workflow": wf, "ref_ids": [1]})
            self.assertEqual(r.status_code, 400, wf)

    def test_video_settings_need_the_video_workflow(self):
        r = self.post({"prompt": "x", "ref_ids": [1], "video_backend": "ltx", "video_size": "story_hd"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("wan_i2v", r.json()["detail"])

    def test_unknown_hires_method_is_refused(self):
        r = self.post({"prompt": "x", "hires": True, "hires_method": "nearest"})
        self.assertEqual(r.status_code, 400)


class StillDelivery(unittest.TestCase):
    def test_render_size_crops_to_4x5(self):
        g = deliver.still_geometry(680, 856, "feed")
        self.assertEqual(g["mode"], "crop")
        self.assertEqual(g["box"], [0, 3, 680, 853])
        self.assertEqual(g["size"], [1080, 1350])

    def test_far_off_ratio_pads(self):
        g = deliver.still_geometry(832, 1216, "feed")
        self.assertEqual(g["mode"], "pad")
        fw, fh = g["fit"]
        self.assertEqual(fh, 1350)
        self.assertLessEqual(fw, 1080)

    def test_wide_source_trims_sides(self):
        g = deliver.still_geometry(1030, 1000, "square")
        self.assertEqual(g["mode"], "crop")
        self.assertEqual(g["box"], [15, 0, 1015, 1000])

    def test_writes_exact_size_jpeg(self):
        with tempfile.TemporaryDirectory() as t:
            src = Path(t) / "s.png"
            Image.new("RGB", (680, 856), (10, 200, 90)).save(src)
            old = deliver.OUT
            deliver.OUT = Path(t)
            try:
                info = deliver.deliver_still(src, "o.jpg", "feed")
            finally:
                deliver.OUT = old
            with Image.open(Path(t) / "o.jpg") as im:
                self.assertEqual((im.format, im.size), ("JPEG", (1080, 1350)))
            self.assertEqual(info["mode"], "crop")


class SecondPassChain(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.execute("ALTER TABLE jobs ADD COLUMN src_image_id INTEGER")
        self.conn.execute("ALTER TABLE jobs ADD COLUMN batch_id TEXT")
        self.addCleanup(self.conn.close)

    def chain(self, params, prompt="cynthia, retro", negative="text"):
        self.conn.execute("INSERT INTO jobs (id, prompt, negative, params, batch_id) VALUES (1,?,?,?, 'b')",
                          (prompt, negative, json.dumps(params)))
        self.conn.execute("INSERT INTO images (job_id, filename) VALUES (1, '1_1_0.png')")
        job = dict(self.conn.execute("SELECT * FROM jobs WHERE id=1").fetchone())
        worker._queue_second_pass(self.conn, job, params)
        return dict(self.conn.execute("SELECT * FROM jobs WHERE id=2").fetchone())

    def test_adds_are_appended_to_pass_two_only(self):
        nxt = self.chain({"second_pass": True, "denoise": 0.45,
                          "second_pass_prompt_add": "platinum blonde hair",
                          "second_pass_negative_add": "dark hair"})
        self.assertEqual(nxt["prompt"], "cynthia, retro, platinum blonde hair")
        self.assertEqual(nxt["negative"], "text, dark hair")
        p = json.loads(nxt["params"])
        self.assertEqual(p["denoise"], 0.65)
        self.assertNotIn("second_pass", p)
        self.assertNotIn("second_pass_prompt_add", p)
        self.assertEqual(nxt["src_image_id"], 1)

    def test_empty_adds_leave_prompt_alone(self):
        nxt = self.chain({"second_pass": True, "second_pass_prompt_add": ""})
        self.assertEqual((nxt["prompt"], nxt["negative"]), ("cynthia, retro", "text"))

    def test_clean_regions_do_not_follow_into_pass_two(self):
        nxt = self.chain({"second_pass": True, "clean_regions": [[0, 0, 0.1, 0.1]],
                          "clean_boxes": [[0, 0, 68, 85]]})
        p = json.loads(nxt["params"])
        self.assertNotIn("clean_regions", p)
        self.assertNotIn("clean_boxes", p)

    def test_hires_carries_to_pass_two(self):
        nxt = self.chain({"second_pass": True, "hires": True, "hires_method": "pixel"})
        p = json.loads(nxt["params"])
        self.assertTrue(p["hires"])
        g, _ = comfy.build(nxt["prompt"], nxt["negative"], p, "pass1.png")
        self.assertIn("31", g)


if __name__ == "__main__":
    unittest.main()
