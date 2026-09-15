"""Reel hook lines, covers, music offset/fade and carousel export. Real ffmpeg."""
import array
import asyncio
import json
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

from app import db as database, deliver, main, reels, worker


def ffprobe(path, entries, stream="v:0"):
    return subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", stream, "-show_entries", entries,
         "-of", "default=nw=1:nk=1", str(path)], capture_output=True, text=True).stdout.split()


def rms(samples):
    return (sum(s * s for s in samples) / max(1, len(samples))) ** 0.5


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for sub in ("out", "refs"):
            (self.root / sub).mkdir()
        for target, attr, value in (
            (reels, "OUT", self.root / "out"), (deliver, "OUT", self.root / "out"),
            (worker, "OUT", self.root / "out"), (worker, "REFS", self.root / "refs"),
            (worker, "THUMBS", self.root / "out"), (database, "DB_PATH", self.root / "t.db"),
        ):
            p = patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        database.init()

    def still(self, name, colour, size=(680, 856)):
        path = self.root / "out" / name
        Image.new("RGB", size, colour).save(path)
        return path


class HookAndCover(Tmp):
    def test_hook_sits_clear_of_instagram_chrome(self):
        card = reels.hook_card("what if Gojo joined Team Rocket: 100% cursed")
        box = card.getchannel("A").getbbox()
        self.assertIsNotNone(box)
        left, top, right, bottom = box
        self.assertGreater(top, 220)                     # below the story header
        self.assertLess(bottom, reels.H - 420)           # above caption/audio credit
        self.assertGreater(left, 40)
        self.assertLess(right, reels.W - 40)

    def test_blank_hook_draws_nothing(self):
        self.assertIsNone(reels.hook_card("   ").getchannel("A").getbbox())

    def test_long_hook_is_truncated_and_wrapped(self):
        card = reels.hook_card("word " * 60)
        _, top, _, bottom = card.getchannel("A").getbbox()
        self.assertLess(bottom - top, 700)

    def test_cover_is_an_exact_story_jpeg(self):
        src = self.still("s.png", (40, 90, 200))
        dst = reels.cover(src, "c.jpg", "hello")
        with Image.open(dst) as im:
            self.assertEqual((im.format, im.size), ("JPEG", (1080, 1920)))


class ReelAudio(Tmp):
    def tone(self, name, silent_first: float, seconds: float):
        """A wav that is silent for `silent_first` seconds, then a loud tone."""
        path = self.root / "refs" / name
        rate = 22050
        data = array.array("h")
        import math
        for i in range(int(rate * seconds)):
            t = i / rate
            data.append(0 if t < silent_first else int(20000 * math.sin(2 * math.pi * 440 * t)))
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(data.tobytes())
        return path

    def audio_of(self, mp4):
        wav = self.root / "a.wav"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(mp4), "-ac", "1", "-ar", "22050",
                        str(wav)], check=True)
        with wave.open(str(wav)) as w:
            return w.getframerate(), array.array("h", w.readframes(w.getnframes()))

    def test_crossfade_reel_fades_music_and_honours_offset_and_hook(self):
        shots = [self.still(f"{i}.png", c) for i, c in enumerate([(200, 0, 0), (0, 200, 0), (0, 0, 200)])]
        track = self.tone("t.wav", silent_first=3.0, seconds=20)
        out = asyncio.run(reels.build(
            shots, "r.mp4", seconds=2.0, transition="crossfade", motion="none",
            audio=track, audio_start=3.0, hook="hook line", hook_seconds=1.5))
        want = reels.total_seconds(2.0, 3, "crossfade")
        got = float(ffprobe(out, "format=duration", "a:0")[0])
        self.assertAlmostEqual(got, want, delta=0.15)
        self.assertEqual(ffprobe(out, "stream=width,height"), ["1080", "1920"])

        rate, pcm = self.audio_of(out)
        start = rms(pcm[: rate // 4])
        middle = rms(pcm[int(rate * 2): int(rate * 2.25)])
        tail = rms(pcm[-rate // 10:])
        # Offset: without -ss the opening quarter-second would be the silent intro.
        self.assertGreater(start, 3000)
        # Fade: the old fade was timed for 6s on a 5s reel and never started.
        self.assertLess(tail, middle * 0.35)

    def test_hook_frames_differ_from_plain_frames(self):
        shots = [self.still(f"{i}.png", (90, 90, 90)) for i in range(2)]
        plain = asyncio.run(reels.build(shots, "p.mp4", seconds=1.0, motion="none"))
        texted = asyncio.run(reels.build(shots, "h.mp4", seconds=1.0, motion="none",
                                         hook="HELLO", hook_seconds=1.0))

        def frame(mp4, at):
            png = self.root / f"{mp4.stem}_{at}.png"
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(at), "-i", str(mp4),
                            "-frames:v", "1", str(png)], check=True)
            return Image.open(png).convert("L")

        from PIL import ImageChops

        def changed(at):
            # Pixels that differ by more than encoder noise.
            d = ImageChops.difference(frame(plain, at), frame(texted, at))
            return sum(d.point(lambda v: 255 if v > 40 else 0).histogram()[255:])

        self.assertGreater(changed(0.5), 5000)   # text is on screen
        self.assertLess(changed(1.7), 50)        # and gone after hook_seconds


class Carousel(Tmp):
    def test_api_validates(self):
        c = TestClient(main.app)
        self.assertEqual(c.post("/api/carousel", json={"shots": []}).status_code, 400)
        self.assertEqual(c.post("/api/carousel", json={
            "shots": [{"id": i} for i in range(21)]}).status_code, 400)
        self.assertEqual(c.post("/api/carousel", json={
            "shots": [{"id": 1}], "target": "reel"}).status_code, 400)
        r = c.post("/api/carousel", json={"shots": [{"id": 1}, {"src": "ref", "id": 2}], "target": "square"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["size"], [1080, 1080])

    def test_slides_render_in_order_at_one_size(self):
        self.still("a.png", (255, 0, 0), (680, 856))
        self.still("b.png", (0, 0, 255), (1024, 1024))
        with database.db() as conn:
            conn.execute("INSERT INTO jobs(id, prompt, params) VALUES (1, 'x', '{}')")
            conn.execute("INSERT INTO images(id, job_id, filename) VALUES (10, 1, 'a.png')")
            conn.execute("INSERT INTO images(id, job_id, filename) VALUES (11, 1, 'b.png')")
            params = {"workflow": "carousel", "target": "feed",
                      "shots": [{"src": "image", "id": 11}, {"src": "image", "id": 10}]}
            conn.execute("INSERT INTO jobs(id, prompt, params) VALUES (2, 'c', ?)", (json.dumps(params),))
            job = dict(conn.execute("SELECT * FROM jobs WHERE id=2").fetchone())
        asyncio.run(worker._run_carousel(job))
        with database.db() as conn:
            names = [r[0] for r in conn.execute("SELECT filename FROM images WHERE job_id=2 ORDER BY id")]
            status = conn.execute("SELECT status FROM jobs WHERE id=2").fetchone()[0]
        self.assertEqual(status, "done")
        self.assertEqual(names, ["2_slide01_ig.jpg", "2_slide02_ig.jpg"])
        sizes = set()
        for n in names:
            with Image.open(self.root / "out" / n) as im:
                sizes.add(im.size)
        self.assertEqual(sizes, {(1080, 1350)})
        # Slide 1 is the blue square still: order follows the pick, not the id.
        with Image.open(self.root / "out" / names[0]) as im:
            self.assertGreater(im.getpixel((540, 675))[2], 200)


class ReelParams(unittest.TestCase):
    def test_reel_request_sanitises_new_fields(self):
        with tempfile.TemporaryDirectory() as t, patch.object(database, "DB_PATH", Path(t) / "x.db"):
            database.init()
            c = TestClient(main.app)
            r = c.post("/api/reels", json={
                "shots": [{"id": 1}, {"id": 2}], "hook": "  a\n  b " + "x" * 200,
                "hook_seconds": 99, "audio_start": -4, "cover_shot": 5})
            self.assertEqual(r.status_code, 200)
            with database.db() as conn:
                p = json.loads(conn.execute("SELECT params FROM jobs").fetchone()[0])
        self.assertTrue(p["hook"].startswith("a b "))
        self.assertEqual(len(p["hook"]), reels.HOOK_MAX)
        self.assertEqual((p["hook_seconds"], p["audio_start"], p["cover_shot"]), (15.0, 0.0, None))


if __name__ == "__main__":
    unittest.main()
