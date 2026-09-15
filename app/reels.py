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
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .config import OUT

W, H = 1080, 1920          # IG reels / stories
FPS = 30

# Installed by the Dockerfile (fonts-dejavu-core); slim images ship no fonts.
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
HOOK_MAX = 90              # characters; a hook is a line, not a caption
# Instagram draws its own UI over a reel: the caption and audio credit along
# the bottom ~420px, the action rail down the right edge, the top ~220px on a
# story. Text placed at 30% height stays clear of all of it.
HOOK_Y = 0.30


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


def hook_card(text: str) -> Image.Image:
    """A full-frame transparent overlay carrying `text`, wrapped and stroked.

    Rendered with PIL rather than ffmpeg's drawtext, whose text= escaping
    (colons, quotes, percent signs, backslashes) breaks on ordinary captions.
    """
    card = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    text = " ".join(text.split())[:HOOK_MAX]
    if not text:
        return card
    size = 84 if len(text) <= 24 else 68 if len(text) <= 48 else 56
    font = ImageFont.truetype(FONT, size)
    lines = textwrap.wrap(text, width=max(8, int(900 / (size * 0.58))))
    draw = ImageDraw.Draw(card)
    gap = int(size * 0.22)
    heights = [draw.textbbox((0, 0), ln, font=font, stroke_width=6)[3] for ln in lines]
    y = int(H * HOOK_Y - (sum(heights) + gap * (len(lines) - 1)) / 2)
    for ln, lh in zip(lines, heights):
        w = draw.textbbox((0, 0), ln, font=font, stroke_width=6)[2]
        draw.text(((W - w) // 2, y), ln, font=font, fill=(255, 255, 255, 255),
                  stroke_width=6, stroke_fill=(10, 12, 16, 235))
        y += lh + gap
    return card


def cover(image: Path, out_name: str, hook: str = "") -> Path:
    """A 1080x1920 cover still: the chosen shot, cover-cropped, with the hook.

    Instagram picks a reel's cover from a frame or an uploaded image; a frame
    grab lands mid-Ken-Burns, so an exact still is the better upload.
    """
    with Image.open(image) as im:
        frame = ImageOps.fit(ImageOps.exif_transpose(im).convert("RGB"), (W, H), Image.LANCZOS)
    if hook.strip():
        frame = Image.alpha_composite(frame.convert("RGBA"), hook_card(hook)).convert("RGB")
    dst = OUT / out_name
    frame.save(dst, "JPEG", quality=92, optimize=True)
    return dst


# How the three phases divide the progress bar. Per-shot encoding is by far
# the slowest - zoompan renders every frame at 1.5x the output size - so it
# owns most of the bar and the join and mux share the tail.
SHOTS_SHARE = 0.75
JOIN_SHARE = 0.92


async def build(images: list[Path], out_name: str, *, bpm: float | None = None,
                beats_per_shot: int = 4, seconds: float = 2.0,
                motion: str = "kenburns", transition: str = "cut",
                audio: Path | None = None, audio_start: float = 0.0,
                hook: str = "", hook_seconds: float = 2.5,
                on_progress=None) -> Path:
    """Assemble `images` into a reel. Returns the written path.

    `audio_start` skips into the track, so the cut lands on the drop rather
    than the intro. `hook` is a line of text over the first `hook_seconds`.

    `on_progress(fraction, stage=...)` is called as each phase advances. There
    is no single ffmpeg invocation to measure here - a reel is one encode per
    shot plus a join - so progress is counted in shots, which is what the
    wait actually consists of.
    """
    if not images:
        raise ReelError("no images given")

    def report(frac, stage):
        if on_progress:
            on_progress(frac, stage=stage)

    dur = shot_seconds(bpm, beats_per_shot, seconds)
    dst = OUT / out_name
    work = OUT / f".reel_{out_name}"
    work.mkdir(exist_ok=True)

    try:
        # 1. Render each still to its own clip.
        clips = []
        for i, img in enumerate(images):
            report(SHOTS_SHARE * i / len(images), f"shot {i + 1} of {len(images)}")
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
        report(SHOTS_SHARE, "joining shots")
        silent = work / "silent.mp4"
        if transition == "crossfade" and len(clips) > 1:
            await _crossfade(clips, dur, silent)
        else:
            lst = work / "list.txt"
            lst.write_text("".join(f"file '{c.name}'\n" for c in clips))
            await _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                        "-i", str(lst), "-c", "copy", str(silent)])

        total = total_seconds(dur, len(clips), transition)

        # 3. Hook text, burned over the opening. One extra encode of the
        #    joined video; the per-shot clips stay text-free.
        if hook.strip():
            report(JOIN_SHARE, "adding the hook line")
            card = work / "hook.png"
            hook_card(hook).save(card)
            shown = max(0.5, min(hook_seconds, total))
            fade = min(0.3, shown / 4)
            texted = work / "texted.mp4"
            await _run([
                "ffmpeg", "-y", "-i", str(silent), "-loop", "1", "-i", str(card),
                "-filter_complex",
                f"[1:v]format=rgba,fade=t=in:st=0:d={fade:.2f}:alpha=1,"
                f"fade=t=out:st={shown - fade:.2f}:d={fade:.2f}:alpha=1[t];"
                f"[0:v][t]overlay=0:0:enable='lte(t,{shown:.2f})':shortest=1,format=yuv420p[v]",
                "-map", "[v]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-pix_fmt", "yuv420p", "-r", str(FPS), str(texted),
            ])
            silent = texted

        # 4. Music, trimmed to the video and faded out. The fade is timed from
        #    the real length: crossfades shorten the reel, and a fade computed
        #    from shots x seconds started after the video had already ended,
        #    so crossfaded reels cut the music off dead instead of fading.
        report(JOIN_SHARE, "adding music" if audio and audio.exists() else "finishing")
        if audio and audio.exists():
            start = max(0.0, float(audio_start or 0))
            await _run([
                "ffmpeg", "-y", "-i", str(silent), "-ss", f"{start:.3f}", "-i", str(audio),
                "-filter_complex", f"[1:a]atrim=0:{total:.3f},asetpts=PTS-STARTPTS,"
                                   f"afade=t=out:st={max(0, total-1.5):.3f}:d=1.5[a]",
                "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac",
                "-b:a", "192k", "-shortest", str(dst),
            ])
        else:
            silent.replace(dst)
        report(1.0, "done")
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
