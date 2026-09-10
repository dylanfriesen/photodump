"""A fake ComfyUI, enough of the API to exercise photodump without a GPU.

kanto has no usable GPU and the render node is a desktop that is asleep most of
the time, so the only way to test this app on demand is to fake the node. This
implements the endpoints app/comfy.py actually calls:

    GET  /system_stats      health probe
    GET  /object_info       checkpoint list + node availability
    GET  /queue             what is running / pending, for re-attach
    GET  /ws?clientId=      per-step progress, addressed to the submitter
    POST /prompt            accept a graph, return a prompt_id
    GET  /history/{id}      report progress, then outputs
    GET  /view              hand back the rendered bytes
    POST /upload/image      accept a reference image

Modes:
    (default)   behaves like a healthy node
    --flaky     UP on /system_stats but hangs up on every /prompt. This is the
                case that distinguishes "PC asleep" from "node is broken", and
                the one that used to requeue forever.
    --latency N seconds a render appears to take (default 3)

Run it on the app's docker network so the container can reach it:

    docker run -d --rm --name mock-comfy --network photodump_default \
      -v "$PWD/tools:/m:ro" photodump-photodump python /m/mock_comfy.py
"""
import argparse
import base64
import hashlib
import io
import json
import queue
import struct
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from PIL import Image

JOBS = {}
SOCKETS = {}        # clientId -> Queue of messages to push down the websocket
ARGS = None
_WEBM = None

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def ws_frame(payload: bytes) -> bytes:
    """A single unfragmented text frame. Server frames are never masked."""
    n = len(payload)
    if n < 126:
        head = struct.pack("!BB", 0x81, n)
    elif n < 65536:
        head = struct.pack("!BBH", 0x81, 126, n)
    else:
        head = struct.pack("!BBQ", 0x81, 127, n)
    return head + payload


def _fake_webm():
    """A real, decodable webm - so downstream re-encoding can be tested.

    Cached: it costs an ffmpeg invocation and never varies.
    """
    global _WEBM
    if _WEBM is None:
        import subprocess, tempfile, os
        path = os.path.join(tempfile.gettempdir(), "mock_clip.webm")
        if not os.path.exists(path):
            subprocess.run(
                ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=544x960:rate=16:duration=3",
                 "-c:v", "libvpx-vp9", "-crf", "40", "-b:v", "0", "-pix_fmt", "yuv420p", path],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(path, "rb") as fh:
            _WEBM = fh.read()
    return _WEBM


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # WebSocket upgrades require HTTP/1.1.
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    # ---------- GET ----------

    def do_GET(self):
        u = urlparse(self.path)

        if u.path == "/system_stats":
            return self._send(200, {
                "system": {"comfyui_version": "mock-0.7.0"},
                "devices": [{"name": "AMD Radeon RX 9070", "vram_total": 17179869184}],
            })

        if u.path == "/object_info":
            info = {
                "CheckpointLoaderSimple": {
                    "input": {"required": {"ckpt_name": [["mock_illustrious.safetensors"]]}}
                },
                "KSampler": {}, "LoadImage": {}, "ImagePadForOutpaint": {},
                "VAEEncodeForInpaint": {}, "VAEEncode": {}, "VAEDecode": {},
                "CLIPTextEncode": {}, "EmptyLatentImage": {}, "SaveImage": {},
                "IPAdapterAdvanced": {},
                "UNETLoader": {"input": {"required": {"unet_name": [["wan2.2_ti2v_5B_fp16.safetensors"]]}}},
                "CLIPLoader": {"input": {"required": {"clip_name": [["umt5_xxl_fp16.safetensors"]]}}},
                "VAELoader": {"input": {"required": {"vae_name": [["wan2.2_vae.safetensors"]]}}},
                "Wan22ImageToVideoLatent": {}, "SaveWEBM": {},
            }
            if not ARGS.no_ipadapter:
                info["IPAdapterUnifiedLoader"] = {}
            if ARGS.no_wan:
                for n in ("UNETLoader", "CLIPLoader", "Wan22ImageToVideoLatent", "SaveWEBM"):
                    info.pop(n, None)
            return self._send(200, info)

        if u.path == "/ws":
            return self._websocket(parse_qs(u.query).get("clientId", [""])[0])

        if u.path == "/queue":
            now = time.time()
            live = [[0, pid, j["graph"], {"client_id": j["client_id"]}] for pid, j in JOBS.items()
                    if now - j["t"] < ARGS.latency]
            return self._send(200, {"queue_running": live[:1],
                                    "queue_pending": live[1:]})

        if u.path.startswith("/history/"):
            return self._history(u.path.rsplit("/", 1)[1])

        if u.path == "/view":
            return self._view(parse_qs(u.query))

        self._send(404, {})

    def _history(self, pid):
        job = JOBS.get(pid)
        if not job:
            return self._send(200, {})
        if time.time() - job["t"] < ARGS.latency:
            return self._send(200, {pid: {"status": {"status_str": "running"}, "outputs": {}}})

        # Video graphs save through node 24 (SaveWEBM); stills through node 9.
        if "24" in job["graph"]:
            node, name = "24", f"{pid}.webm"
        else:
            node, name = "9", f"{pid}.png"
        return self._send(200, {pid: {
            "status": {"status_str": "success"},
            "outputs": {node: {"images": [{"filename": name, "subfolder": "", "type": "output"}]}},
        }})

    def _websocket(self, client_id):
        """Minimal server-to-client websocket.

        Real ComfyUI addresses progress to the client id that submitted the
        prompt, so a test that does not model that would not exercise the
        thing most likely to break. Only the server-to-client direction is
        implemented - nothing here ever reads a frame.
        """
        key = self.headers.get("Sec-WebSocket-Key")
        if not key or not client_id:
            return self._send(400, {})
        accept = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.wfile.flush()

        q = queue.Queue()
        SOCKETS[client_id] = q
        self.close_connection = True   # this thread owns the socket from here
        self.wfile.write(ws_frame(json.dumps(
            {"type": "status", "data": {"status": {"exec_info": {"queue_remaining": 0}}}}).encode()))
        try:
            while True:
                msg = q.get(timeout=ARGS.latency * 4 + 20)
                if msg is None:
                    return
                self.wfile.write(ws_frame(json.dumps(msg).encode()))
                self.wfile.flush()
        except (queue.Empty, BrokenPipeError, OSError):
            return
        finally:
            if SOCKETS.get(client_id) is q:
                SOCKETS.pop(client_id, None)

    def _emit_progress(self, client_id, graph, steps):
        """Walk the sampler's steps over the render window, as ComfyUI does."""
        def emit(packet):
            q = SOCKETS.get(client_id)
            if q is not None:
                q.put(packet)
        gap = max(0.02, ARGS.latency * 0.8 / max(1, steps))
        emit({"type": "executing", "data": {"node": "3"}})
        for i in range(1, steps + 1):
            time.sleep(gap)
            emit({"type": "progress", "data": {"value": i, "max": steps, "node": "3"}})
        emit({"type": "executing", "data": {"node": "8"}})   # VAEDecode
        time.sleep(ARGS.latency * 0.1)
        emit({"type": "executing", "data": {"node": None}})

    def _view(self, q):
        fn = q.get("filename", [""])[0]
        if fn.endswith(".webm"):
            return self._send(200, _fake_webm(), "video/webm")
        buf = io.BytesIO()
        Image.new("RGB", (832, 1216), (124, 92, 255)).save(buf, "PNG")
        return self._send(200, buf.getvalue(), "image/png")

    # ---------- POST ----------

    def do_POST(self):
        u = urlparse(self.path)

        if ARGS.flaky:
            print(f"  [mock] FLAKY: hanging up on {u.path}", flush=True)
            self.close_connection = True
            self.wfile.close()
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        if u.path == "/prompt":
            graph = json.loads(body).get("prompt", {})
            assert "3" in graph and graph["3"]["class_type"] == "KSampler", "graph has no KSampler"
            pid = uuid.uuid4().hex
            JOBS[pid] = {"t": time.time(), "graph": graph,
                         "client_id": json.loads(body).get("client_id", "")}
            self._describe(pid, graph)
            client_id = json.loads(body).get("client_id", "")
            threading.Thread(
                target=self._emit_progress,
                args=(client_id, graph, int(graph["3"]["inputs"]["steps"])),
                daemon=True).start()
            return self._send(200, {"prompt_id": pid})

        if u.path == "/upload/image":
            print("  [mock] reference upload received", flush=True)
            return self._send(200, {"name": "uploaded_ref.png", "subfolder": "", "type": "input"})

        self._send(404, {})

    def _describe(self, pid, graph):
        k = graph["3"]["inputs"]
        ckpt = graph.get("4", {}).get("inputs", {}).get("ckpt_name", "-")
        print(f"  [mock] accept {pid[:8]} ckpt={ckpt} seed={k['seed']} steps={k['steps']}", flush=True)
        if "14" in graph:  # ImagePadForOutpaint
            i = graph["14"]["inputs"]
            print(f"  [mock]   PAD l={i['left']} r={i['right']} t={i['top']} b={i['bottom']}", flush=True)
        if "23" in graph:  # Wan22ImageToVideoLatent
            i = graph["23"]["inputs"]
            print(f"  [mock]   VIDEO {i['width']}x{i['height']} frames={i['length']} "
                  f"unet={graph['20']['inputs']['unet_name']}", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8188)
    ap.add_argument("--latency", type=float, default=3.0)
    ap.add_argument("--flaky", action="store_true", help="up, but crashes on every submit")
    ap.add_argument("--no-ipadapter", action="store_true", help="pretend the node pack is missing")
    ap.add_argument("--no-wan", action="store_true", help="pretend the WAN video nodes are missing")
    ARGS = ap.parse_args()
    print(f"mock ComfyUI on :{ARGS.port} (latency={ARGS.latency}s flaky={ARGS.flaky})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", ARGS.port), Handler).serve_forever()
