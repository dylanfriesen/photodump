"""LoRA support: graph injection, request validation, and the offline node list."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app import comfy, db as database, main, preflight, worker

TWO = [{"name": "zzz/evelyn.safetensors", "strength": 0.8},
       {"name": "retro_cel.safetensors", "strength": 0.5, "strength_clip": 1.0}]

# Every image graph, with the references it needs to take its real path.
IMAGE_MODES = [
    ({"workflow": "txt2img"}, []),
    ({"workflow": "txt2img", "hires": True}, []),
    ({"workflow": "img2img"}, ["r.png"]),
    ({"workflow": "img2img", "hires": True, "hires_method": "pixel"}, ["r.png"]),
    ({"workflow": "img2img", "hires": True, "hires_method": "latent"}, ["r.png"]),
    ({"workflow": "ipadapter"}, ["r.png"]),
    ({"workflow": "ipadapter_multi"}, ["r.png", "s.png"]),
    ({"workflow": "outpaint"}, ["r.png"]),
]


def refs_to(graph, node_id, slot):
    return [(nid, key) for nid, n in graph.items() for key, v in n["inputs"].items()
            if isinstance(v, list) and len(v) == 2 and v == [node_id, slot]]


class GraphInjection(unittest.TestCase):
    def test_no_loras_leaves_every_graph_unchanged(self):
        for params, refs in IMAGE_MODES:
            with self.subTest(**params):
                a, _ = comfy.build("p", "n", {**params, "seed": 5}, refs)
                b, _ = comfy.build("p", "n", {**params, "seed": 5, "loras": []}, refs)
                self.assertEqual(a, b)
                self.assertNotIn("LoraLoader", {n["class_type"] for n in a.values()})

    def test_every_model_and_clip_consumer_goes_through_the_last_lora(self):
        for params, refs in IMAGE_MODES:
            with self.subTest(**params):
                g, _ = comfy.build("p", "n", {**params, "loras": TWO}, refs)
                self.assertEqual(g["40"]["class_type"], "LoraLoader")
                self.assertEqual(g["41"]["class_type"], "LoraLoader")
                # Chain: checkpoint -> 40 -> 41.
                self.assertEqual(g["40"]["inputs"]["model"], ["4", 0])
                self.assertEqual(g["40"]["inputs"]["clip"], ["4", 1])
                self.assertEqual(g["41"]["inputs"]["model"], ["40", 0])
                self.assertEqual(g["41"]["inputs"]["clip"], ["40", 1])
                # Nothing but the first loader reads MODEL or CLIP from the checkpoint.
                self.assertEqual(refs_to(g, "4", 0), [("40", "model")])
                self.assertEqual(refs_to(g, "4", 1), [("40", "clip")])
                # VAE still comes straight from the checkpoint.
                self.assertTrue(refs_to(g, "4", 2))
                # Both prompts are encoded with the LoRA'd CLIP.
                self.assertEqual(g["6"]["inputs"]["clip"], ["41", 1])
                self.assertEqual(g["7"]["inputs"]["clip"], ["41", 1])

    def test_hires_second_sampler_and_ipadapter_pick_it_up(self):
        g, _ = comfy.build("p", "n", {"workflow": "txt2img", "hires": True, "loras": TWO})
        self.assertEqual(g["3"]["inputs"]["model"], ["41", 0])
        self.assertEqual(g["31"]["inputs"]["model"], ["41", 0])
        g, _ = comfy.build("p", "n", {"workflow": "ipadapter", "loras": TWO}, ["r.png"])
        # LoRA beneath IP-Adapter: the adapter loader patches the LoRA'd model.
        self.assertEqual(g["12"]["inputs"]["model"], ["41", 0])
        self.assertEqual(g["3"]["inputs"]["model"], ["13", 0])

    def test_strengths_and_names_are_carried(self):
        g, _ = comfy.build("p", "n", {"workflow": "txt2img", "loras": TWO})
        self.assertEqual(g["40"]["inputs"]["lora_name"], "zzz/evelyn.safetensors")
        self.assertEqual((g["40"]["inputs"]["strength_model"], g["40"]["inputs"]["strength_clip"]), (0.8, 0.8))
        self.assertEqual((g["41"]["inputs"]["strength_model"], g["41"]["inputs"]["strength_clip"]), (0.5, 1.0))

    def test_video_graphs_never_get_loras(self):
        g, _ = comfy.build("p", "n", {"workflow": "wan_i2v", "video_backend": "wan", "loras": TWO}, ["r.png"])
        self.assertNotIn("LoraLoader", {n["class_type"] for n in g.values()})

    def test_colliding_node_id_is_refused_not_overwritten(self):
        wf = {"4": {"class_type": "CheckpointLoaderSimple", "inputs": {}},
              "40": {"class_type": "Something", "inputs": {}}}
        with self.assertRaises(comfy.ComfyError):
            comfy.apply_loras(wf, TWO[:1])


class ComboShapes(unittest.TestCase):
    def test_both_schema_shapes(self):
        info = {
            "CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": [["a.safetensors", "b.safetensors"]]}}},
            "LoraLoader": {"input": {"required": {"lora_name": ["COMBO", {"options": ["x.safetensors"]}]}}},
            "Broken": {"input": {"required": {"f": "COMBO"}}},
        }
        self.assertEqual(comfy.combo_options(info, "CheckpointLoaderSimple", "ckpt_name"),
                         ["a.safetensors", "b.safetensors"])
        self.assertEqual(comfy.combo_options(info, "LoraLoader", "lora_name"), ["x.safetensors"])
        self.assertIsNone(comfy.combo_options(info, "Broken", "f"))
        self.assertIsNone(comfy.combo_options(info, "Missing", "f"))

    def test_preflight_reads_the_new_shape(self):
        info = {"LoraLoader": {"input": {"required": {"lora_name": ["COMBO", {"options": ["x"]}]}}}}
        self.assertEqual(preflight._options(info, "LoraLoader", "lora_name"), ["x"])


class Requests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = patch.object(database, "DB_PATH", Path(self.tmp.name) / "t.db")
        p.start()
        self.addCleanup(p.stop)
        database.init()
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)

    def stored(self):
        with database.db() as conn:
            return [json.loads(r[0]) for r in conn.execute("SELECT params FROM jobs ORDER BY id")]

    def test_valid_loras_are_stored_clamped(self):
        r = self.client.post("/api/generate", json={"prompt": "x", "loras": [
            {"name": " a.safetensors ", "strength": 3},
            {"name": "", "strength": 1},                         # empty picker row
            {"name": "sub\\b.safetensors", "strength": -0.5, "strength_clip": 0.2}]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.stored()[0]["loras"], [
            {"name": "a.safetensors", "strength": 2.0},
            {"name": "sub\\b.safetensors", "strength": -0.5, "strength_clip": 0.2}])

    def test_bad_requests_write_nothing(self):
        bad = [
            {"loras": "a.safetensors"},
            {"loras": [{"name": "../../secrets.safetensors"}]},
            {"loras": [{"name": "/abs.safetensors"}]},
            {"loras": [{"name": "a", "strength": "strong"}]},
            {"loras": [{"name": f"{i}"} for i in range(5)]},
            {"loras": [{"name": "a"}], "workflow": "wan_i2v", "ref_ids": [1]},
        ]
        for body in bad:
            with self.subTest(body=body):
                self.assertEqual(self.client.post("/api/generate", json={"prompt": "x", **body}).status_code, 400)
        self.assertEqual(self.stored(), [])

    def test_style_match_pass_two_keeps_the_loras(self):
        self.client.post("/api/generate", json={"prompt": "x", "workflow": "img2img", "ref_ids": [1],
                                                "second_pass": True, "loras": TWO})
        with database.db() as conn:
            conn.execute("UPDATE jobs SET status='done' WHERE id=1")
            conn.execute("INSERT INTO images(job_id, filename) VALUES (1, 'a.png')")
            job = dict(conn.execute("SELECT * FROM jobs WHERE id=1").fetchone())
            with patch.object(worker, "wake"):
                worker._queue_second_pass(conn, job, json.loads(job["params"]))
        self.assertEqual(self.stored()[1]["loras"], self.stored()[0]["loras"])

    def test_sweep_cells_can_vary_loras(self):
        r = self.client.post("/api/sweeps", json={
            "name": "lora-strength", "seeds": [1],
            "base": {"prompt": "evelyn chevalier"},
            "cells": [{"label": "none", "set": {"loras": []}},
                      {"label": "0.6", "set": {"loras": [{"name": "e.safetensors", "strength": 0.6}]}}]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([p["loras"] for p in self.stored()], [[], [{"name": "e.safetensors", "strength": 0.6}]])

    def test_node_list_is_cached_for_when_the_desktop_sleeps(self):
        live = {"checkpoints": ["c"], "loras": ["x.safetensors"], "has_ipadapter": True, "node_count": 9}
        with patch.object(comfy, "available", AsyncMock(return_value=live)):
            self.assertEqual(self.client.get("/api/node").json()["loras"], ["x.safetensors"])
        with patch.object(comfy, "available", AsyncMock(side_effect=comfy.ComfyOffline("asleep"))):
            r = self.client.get("/api/node")
        self.assertEqual(r.status_code, 503)
        body = r.json()
        self.assertTrue(body["offline"])
        self.assertEqual(body["loras"], ["x.safetensors"])
        self.assertIn("cached_at", body)

    def test_offline_with_no_cache_says_so(self):
        with patch.object(comfy, "available", AsyncMock(side_effect=comfy.ComfyOffline("asleep"))):
            body = self.client.get("/api/node").json()
        self.assertEqual((body["offline"], "loras" in body), (True, False))


if __name__ == "__main__":
    unittest.main()
