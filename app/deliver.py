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


async def deliver(src: Path, out_name: str, target: str = "reel") -> dict:
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
