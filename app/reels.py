"""Reel assembly: stills -> a 9:16 video, built locally with ffmpeg.

This deliberately runs on kanto rather than the render node. It needs no GPU,
so reels assemble even while the desktop is asleep - which is most of the time.

On "beat sync": real onset detection would mean librosa (numpy/scipy/numba, a
very large dependency) for something the user already knows. Instead cuts are
driven by an explicit BPM, which is deterministic and lands exactly on the beat
when the BPM is right. Hard cuts on the beat are what reads as "synced" anyway;
crossfades soften precisely the moment you are trying to hit.
"""
import asyncio
import shlex
from pathlib import Path

from .config import OUT

W, H = 1080, 1920          # IG reels / stories
FPS = 30


class ReelError(Exception):
    pass


async def _run(cmd: list[str]):
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        tail = err.decode(errors="replace").strip().splitlines()[-6:]
        raise ReelError(f"ffmpeg failed: {' | '.join(tail)}\ncmd: {shlex.join(cmd)}")


def fade_seconds(shot: float) -> float:
    """Crossfade length for a given shot length. Mirrors _crossfade()."""
    return min(0.5, shot / 3)


def total_seconds(shot: float, count: int, transition: str) -> float:
    """Finished length. Crossfades overlap, so they shorten the reel."""
    if transition == "crossfade" and count > 1:
        return shot * count - fade_seconds(shot) * (count - 1)
    return shot * count


def shot_frames(seconds: float) -> int:
    return max(2, int(round(seconds * FPS)))


def shot_seconds(bpm: float | None, beats_per_shot: int, fallback: float) -> float:
    """Seconds each still is held. BPM wins when supplied."""
    if bpm and bpm > 0:
        return (60.0 / bpm) * max(1, beats_per_shot)
    return max(0.4, fallback)


def _kenburns(index: int, seconds: float, motion: str) -> str:
    """Ken Burns as an ffmpeg filter chain.

    zoompan's `d` is how many output frames each *input* frame becomes. With a
    looped input, d=frames multiplies out to frames*fps*seconds of work - which
    is minutes of encoding for a few seconds of video. So d=1 (one output frame
    per input frame) and the motion is driven by `on`, the output frame counter.
    The caller bounds the shot with -frames:v.
    """
    frames = shot_frames(seconds)
    # Modest oversample so zoompan has detail to interpolate into without
    # paying for 4x the pixels.
    pw, ph = int(W * 1.5), int(H * 1.5)
    pre = (f"scale={pw}:{ph}:force_original_aspect_ratio=increase,"
           f"crop={pw}:{ph}")

    if motion == "none":
        z, x, y = "1", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    elif index % 2 == 0:      # alternate direction so a run of shots has rhythm
        z = f"1+0.12*on/{frames}"
        x, y = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    else:
        z = f"1.12-0.12*on/{frames}"
        x, y = "iw/2-(iw/zoom/2)", f"ih/2-(ih/zoom/2)+({H}/8)*on/{frames}"

    return (f"{pre},zoompan=z='{z}':x='{x}':y='{y}':d=1:s={W}x{H}:fps={FPS},"
            f"setsar=1,format=yuv420p")


async def build(images: list[Path], out_name: str, *, bpm: float | None = None,
                beats_per_shot: int = 4, seconds: float = 2.0,
                motion: str = "kenburns", transition: str = "cut",
                audio: Path | None = None) -> Path:
    """Assemble `images` into a reel. Returns the written path."""
    if not images:
        raise ReelError("no images given")

    dur = shot_seconds(bpm, beats_per_shot, seconds)
    dst = OUT / out_name
    work = OUT / f".reel_{out_name}"
    work.mkdir(exist_ok=True)

    try:
        # 1. Render each still to its own clip.
        clips = []
        for i, img in enumerate(images):
            clip = work / f"{i:03d}.mp4"
            await _run([
                "ffmpeg", "-y", "-loop", "1", "-framerate", str(FPS), "-i", str(img),
                "-vf", _kenburns(i, dur, motion),
                "-frames:v", str(shot_frames(dur)),
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-pix_fmt", "yuv420p", "-r", str(FPS), str(clip),
            ])
            clips.append(clip)

        # 2. Join. Hard cuts use the concat demuxer (exact, no re-encode drift);
        #    crossfades need a filter chain, so they are built separately.
        silent = work / "silent.mp4"
        if transition == "crossfade" and len(clips) > 1:
            await _crossfade(clips, dur, silent)
        else:
            lst = work / "list.txt"
            lst.write_text("".join(f"file '{c.name}'\n" for c in clips))
            await _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                        "-i", str(lst), "-c", "copy", str(silent)])

        # 3. Music, trimmed to the video and faded out.
        if audio and audio.exists():
            total = dur * len(clips)
            await _run([
                "ffmpeg", "-y", "-i", str(silent), "-i", str(audio),
                "-filter_complex", f"[1:a]atrim=0:{total:.3f},afade=t=out:st={max(0, total-1.5):.3f}:d=1.5[a]",
                "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac",
                "-b:a", "192k", "-shortest", str(dst),
            ])
        else:
            silent.replace(dst)
        return dst
    finally:
        for f in work.glob("*"):
            f.unlink(missing_ok=True)
        work.rmdir()


async def _crossfade(clips: list[Path], dur: float, dst: Path):
    """Chain xfade across clips. Each transition eats into the shot length."""
    fade = fade_seconds(dur)
    inputs = []
    for c in clips:
        inputs += ["-i", str(c)]

    chain, prev, offset = [], "[0:v]", dur - fade
    for i in range(1, len(clips)):
        label = f"[v{i}]"
        chain.append(f"{prev}[{i}:v]xfade=transition=fade:duration={fade:.3f}:"
                     f"offset={offset:.3f}{label}")
        prev = label
        offset += dur - fade
    await _run(["ffmpeg", "-y", *inputs, "-filter_complex", ";".join(chain),
                "-map", prev, "-c:v", "libx264", "-preset", "veryfast",
                "-crf", "20", "-pix_fmt", "yuv420p", str(dst)])
