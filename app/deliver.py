"""Instagram delivery encode. Runs on kanto - no GPU involved.

Generation and delivery are different jobs. The render node makes something
small and cheap (a 544x960 webm at 16fps, say); this turns it into a file
Instagram will accept and re-encode kindly.

Two non-obvious rules, both from Instagram's own behaviour:

* **MP4/H.264 only.** Reels reject webm, which is exactly what ComfyUI's
  SaveWEBM produces.
* **Do not max the bitrate.** Instagram re-encodes everything and punishes
  high-bitrate sources; a controlled 5-8 Mbps survives that pass looking
  sharper than a 20 Mbps upload does. So this caps rather than chases.
"""
import asyncio
import shlex
from pathlib import Path

from PIL import Image, ImageOps

from .config import OUT

# 1080 wide is Instagram's ceiling - anything larger is downscaled on upload,
# so rendering beyond it only wastes GPU time.
TARGETS = {
    "reel":   (1080, 1920),   # 9:16 reels / stories
    "feed":   (1080, 1350),   # 4:5, the tallest the feed allows
    "square": (1080, 1080),
}
BITRATE = "6500k"     # middle of the 5-8 Mbps window that survives re-encode
MAXRATE = "8000k"
BUFSIZE = "12000k"
MIN_FPS = 24          # below this Instagram playback reads as juddery


class DeliverError(Exception):
    pass


async def _watch(cmd: list[str], seconds: float, on_progress) -> None:
    """Run an encode, reporting real progress from ffmpeg's own counter.

    `-progress pipe:1` prints `key=value` lines as it encodes; `out_time_us`
    against the known source duration is an exact percentage, not a guess.
    stderr is drained concurrently - ffmpeg fills that pipe and blocks if
    nobody reads it.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

    async def pump():
        async for raw in proc.stdout:
            key, _, value = raw.decode(errors="replace").strip().partition("=")
            if key in ("out_time_us", "out_time_ms") and seconds > 0:
                try:
                    done = int(value) / 1_000_000
                except ValueError:
                    continue
                on_progress(min(0.99, done / seconds), stage="encoding for Instagram")

    err_chunks: list[bytes] = []

    async def drain():
        err_chunks.append(await proc.stderr.read())

    await asyncio.gather(pump(), drain())
    await proc.wait()
    if proc.returncode != 0:
        tail = b"".join(err_chunks).decode(errors="replace").strip().splitlines()[-6:]
        raise DeliverError(f"ffmpeg failed: {' | '.join(tail)}\ncmd: {shlex.join(cmd)}")


async def _run(cmd: list[str]) -> str:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode != 0:
        tail = err.decode(errors="replace").strip().splitlines()[-6:]
        raise DeliverError(f"ffmpeg failed: {' | '.join(tail)}\ncmd: {shlex.join(cmd)}")
    return out.decode(errors="replace").strip()


async def probe(path: Path) -> dict:
    out = await _run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate",
        "-show_entries", "format=duration", "-of", "default=nw=1", str(path)])
    d = {}
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            d[k] = v
    num, _, den = d.get("r_frame_rate", "0/1").partition("/")
    try:
        d["fps"] = float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        d["fps"] = 0.0
    d["has_audio"] = bool(await _run([
        "ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
        "stream=index", "-of", "csv=p=0", str(path)]))
    return d


# A still this close to the target ratio is cropped rather than padded. The
# renders come out 680x856 - 0.794 against 4:5's 0.800 - and six pixels of
# crop beats two slivers of letterbox. Past this, cropping starts cutting off
# the subject, so it pads like the video path does.
STILL_CROP_MAX = 0.04


def still_geometry(w: int, h: int, target: str) -> dict:
    """How a w x h still maps onto a target canvas: crop box, then scale.

    Pure, so the decision can be tested without an image.
    """
    tw, th = TARGETS.get(target, TARGETS["feed"])
    want, have = tw / th, w / h
    loss = 1 - min(want / have, have / want)
    if loss <= STILL_CROP_MAX:
        if have > want:                       # too wide: trim the sides
            cw, ch = round(h * want), h
        else:                                 # too tall: trim top and bottom
            cw, ch = w, round(w / want)
        x, y = (w - cw) // 2, (h - ch) // 2
        return {"mode": "crop", "box": [x, y, x + cw, y + ch], "size": [tw, th]}
    scale = min(tw / w, th / h)
    fw, fh = round(w * scale), round(h * scale)
    return {"mode": "pad", "fit": [fw, fh],
            "offset": [(tw - fw) // 2, (th - fh) // 2], "size": [tw, th]}


def deliver_still(src: Path, out_name: str, target: str = "feed") -> dict:
    """Instagram-ready JPEG from a still. Returns what it did.

    JPEG because Instagram converts everything to it anyway; handing it a
    q95 sRGB JPEG at its exact size leaves its converter nothing to decide
    except its own recompression. Scaling to 1080
    wide is interpolation, not detail - the upscale tail on img2img is what
    adds real resolution; this only stops Instagram doing the resize itself.
    """
    if not src.exists():
        raise DeliverError(f"{src.name} no longer exists")
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
    w, h = im.size
    g = still_geometry(w, h, target)
    tw, th = g["size"]
    if g["mode"] == "crop":
        out = im.crop(tuple(g["box"])).resize((tw, th), Image.LANCZOS)
    else:
        out = Image.new("RGB", (tw, th), (0, 0, 0))
        out.paste(im.resize(tuple(g["fit"]), Image.LANCZOS), tuple(g["offset"]))
    dst = OUT / out_name
    out.save(dst, "JPEG", quality=95, subsampling=0, optimize=True)
    return {
        "filename": out_name,
        "target": target,
        "size": [tw, th],
        "mode": g["mode"],
        "bytes": dst.stat().st_size,
        "upscaled_from": [w, h],
    }


async def deliver(src: Path, out_name: str, target: str = "reel",
                  on_progress=None) -> dict:
    """Re-encode `src` into an Instagram-ready mp4. Returns what it did."""
    if not src.exists():
        raise DeliverError(f"{src.name} no longer exists")
    w, h = TARGETS.get(target, TARGETS["reel"])
    info = await probe(src)
    dst = OUT / out_name

    # Fit inside the target then pad, so nothing is cropped and the aspect
    # is exact. Instagram letterboxes far more gracefully than it crops.
    vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos,"
          f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,format=yuv420p")

    src_fps = info.get("fps") or 0
    out_fps = src_fps if src_fps >= MIN_FPS else 30
    if src_fps and src_fps < MIN_FPS:
        # Duplicating frames satisfies the container without inventing motion;
        # the judder is in the source and honest upscaling cannot remove it.
        vf += f",fps={out_fps}"

    cmd = ["ffmpeg", "-y", "-i", str(src), "-vf", vf,
           "-c:v", "libx264", "-profile:v", "high", "-level", "4.1",
           "-preset", "slow", "-b:v", BITRATE, "-maxrate", MAXRATE,
           "-bufsize", BUFSIZE, "-pix_fmt", "yuv420p",
           "-r", str(int(round(out_fps))), "-movflags", "+faststart"]
    if info["has_audio"]:
        cmd += ["-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
    else:
        cmd += ["-an"]
    cmd += [str(dst)]
    duration = float(info.get("duration") or 0)
    if on_progress and duration > 0:
        await _watch(["ffmpeg", "-progress", "pipe:1", "-nostats", *cmd[1:]],
                     duration, on_progress)
        on_progress(1.0, stage="done")
    else:
        await _run(cmd)

    final = await probe(dst)
    return {
        "filename": out_name,
        "target": target,
        "size": [int(final.get("width", 0)), int(final.get("height", 0))],
        "fps": round(final.get("fps", 0), 2),
        "seconds": round(float(final.get("duration", 0) or 0), 2),
        "bytes": dst.stat().st_size,
        "upscaled_from": [int(info.get("width", 0)), int(info.get("height", 0))],
        "fps_padded": src_fps < MIN_FPS,
    }
