"""Metadata used by the v2 detail view and style-match queue, without a GPU."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import db as database, main


class DesignAPI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = patch.object(database, "DB_PATH", Path(self.tmp.name) / "test.db")
        self.path.start()
        self.addCleanup(self.path.stop)
        database.init()
        # No lifespan context: do not start the render worker.
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)
        with database.db() as conn:
            conn.execute(
                "INSERT INTO jobs(id,prompt,negative,params,ref_id,ref_ids,batch_id,status) "
                "VALUES(1,'portrait','lettering',?,7,'[7]','pair','done')",
                (json.dumps({"free_prompt": True, "second_pass": True, "steps": 40}),))
            conn.execute("INSERT INTO images(id,job_id,filename) VALUES(10,1,'first.png')")
            conn.execute("INSERT INTO images(id,job_id,filename) VALUES(11,1,'latest.png')")
            conn.execute(
                "INSERT INTO jobs(id,prompt,params,src_image_id,batch_id) "
                "VALUES(2,'colour pass',?,11,'pair')",
                (json.dumps({"workflow": "img2img", "free_prompt": True}),))

    def test_queue_has_parent_link_and_latest_thumbnail(self):
        response = self.client.get("/api/jobs")
        self.assertEqual(response.status_code, 200)
        second, first = response.json()
        self.assertEqual(second["src_job_id"], first["id"])
        self.assertEqual(first["out_filename"], "latest.png")
        self.assertEqual(second["batch_id"], first["batch_id"])
        self.assertIsNone(second["out_filename"])

    def test_detail_has_saved_prompt_reference_and_sampler(self):
        image = self.client.get("/api/images?limit=1").json()[0]
        self.assertEqual(image["negative"], "lettering")
        self.assertEqual(image["ref_ids"], "[7]")
        self.assertEqual(image["ref_id"], 7)
        self.assertIsNone(image["src_image_id"])
        self.assertEqual(json.loads(image["params"])["steps"], 40)

    def test_limits_rejected_before_database_access(self):
        with patch.object(main, "db", side_effect=AssertionError("must not query")):
            for endpoint in ("jobs", "images"):
                for limit in ("-1", "0", "501", "invalid"):
                    with self.subTest(endpoint=endpoint, limit=limit):
                        self.assertEqual(self.client.get(f"/api/{endpoint}?limit={limit}").status_code, 422)

    def test_schedule_round_trip_and_clear(self):
        url = "/api/jobs/2/schedule"
        when = "2099-09-15 08:30:00"
        self.assertEqual(self.client.post(url, json={"not_before": when}).json()["not_before"], when)
        self.assertEqual(self.client.get("/api/jobs").json()[0]["not_before"], when)
        self.assertEqual(self.client.post(url, json={"not_before": None}).status_code, 200)
        self.assertIsNone(self.client.get("/api/jobs").json()[0]["not_before"])


if __name__ == "__main__":
    unittest.main()
