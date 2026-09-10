"""Live progress for the job currently in flight.

The worker drains one job at a time, so a single in-memory slot is all this
needs - no table, no per-job rows to clean up. It is deliberately lossy: a
restart forgets what was in flight, which is correct, because a restart also
requeues the job.

Three sources feed it, in descending order of honesty:

* **steps** - ComfyUI's own per-sampler-step count, over the websocket.
* **ffmpeg** - `-progress` output from reel and delivery encodes, which run
  here on kanto and have a known output duration.
* **elapsed** - nothing has reported yet, so the bar is driven by how long
  jobs of this kind have historically taken. Marked as an estimate in the UI
  so a slow job does not look like a stuck one.

The sampler is not the whole render: VAE decode, video encode and the fetch
back to kanto all happen after the last step. So steps map onto 0-SAMPLER_CEIL
rather than 0-100, and the tail is reported as its own stage. A bar that sits
at 92% while something real is happening beats one that hits 100% and lies.
"""
import time

from .db import db

SAMPLER_CEIL = 92.0   # what "all sampler steps done" is worth, in percent
TAIL_PERCENT = 96.0   # decode / encode / fetch, after the last step

_cur: dict | None = None


def start(job_id: int, kind: str, *, stage: str = "starting") -> None:
    global _cur
    _cur = {
        "job_id": job_id,
        "kind": kind,
        "stage": stage,
        "source": "elapsed",
        "value": 0,
        "max": 0,
        "percent": None,
        "started": time.monotonic(),
        "estimate": typical_seconds(kind),
    }


def step(value: int, total: int, *, stage: str | None = None,
         phase: int = 0, phases: int = 1) -> None:
    """A sampler step landed. `total` is the step count for this node."""
    if _cur is None or not total:
        return
    _cur.update(
        source="steps",
        value=int(value),
        max=int(total),
        percent=min(SAMPLER_CEIL, ((phase + max(0, value / total)) / max(1, phases)) * SAMPLER_CEIL),
    )
    if stage:
        _cur["stage"] = stage


def fraction(frac: float, *, stage: str | None = None, source: str = "ffmpeg") -> None:
    """0..1 of the whole job, from something that knows its own total."""
    if _cur is None:
        return
    _cur.update(source=source, percent=max(0.0, min(100.0, frac * 100)))
    if stage:
        _cur["stage"] = stage


def stage(text: str, *, tail: bool = False) -> None:
    """Name what is happening now. `tail` means the sampler is finished."""
    if _cur is None:
        return
    _cur["stage"] = text
    if tail:
        _cur.update(percent=max(_cur.get("percent") or 0, TAIL_PERCENT), source="stage")


def done() -> None:
    global _cur
    _cur = None


def resume(created_ms: float | None, kind: str | None = None) -> None:
    """Retain the node's elapsed time when reconnecting after an app restart."""
    if _cur is None:
        return
    if isinstance(created_ms, (float, int)):
        _cur["started"] = time.monotonic() - max(0, time.time() - created_ms / 1000)
    if kind:
        _cur["kind"] = kind
    _cur["estimate"] = None  # A different submitted graph may have a different cost.


def snapshot() -> dict | None:
    """What /api/status publishes. None when nothing is in flight."""
    if _cur is None:
        return None
    elapsed = time.monotonic() - _cur["started"]
    pct = _cur["percent"]
    estimated = False

    if pct is None:
        # Nothing has reported yet. Fall back to how long this kind of job
        # usually takes, and never let the guess reach the end on its own.
        est = _cur["estimate"]
        pct = min(90.0, (elapsed / est) * 100) if est else None
        estimated = True

    eta = None
    if pct and pct > 3 and elapsed > 3:
        eta = max(0.0, elapsed * (100 - pct) / pct)
    elif estimated is False and _cur["estimate"]:
        eta = max(0.0, _cur["estimate"] - elapsed)

    return {
        "job_id": _cur["job_id"],
        "kind": _cur["kind"],
        "stage": _cur["stage"],
        "source": _cur["source"],
        "step": _cur["value"],
        "steps": _cur["max"],
        "percent": round(pct, 1) if pct is not None else None,
        "estimated": estimated,
        "elapsed": round(elapsed, 1),
        "eta": round(eta) if eta is not None else None,
    }


def typical_seconds(kind: str) -> float | None:
    """Median wall time of the last few finished jobs of this kind.

    Used only for the pre-first-step estimate. Returns None until this kind
    has actually completed once, so a first run shows no fabricated bar.
    """
    try:
        with db() as conn:
            rows = conn.execute(
                "SELECT (julianday(finished_at) - julianday(started_at)) * 86400 AS secs "
                "FROM jobs WHERE status='done' AND started_at IS NOT NULL "
                "AND finished_at IS NOT NULL "
                "AND COALESCE(json_extract(params, '$.workflow'), 'txt2img') = ? "
                "ORDER BY id DESC LIMIT 7",
                (kind,),
            ).fetchall()
    except Exception:
        return None
    secs = sorted(r["secs"] for r in rows if r["secs"] and r["secs"] > 0)
    if not secs:
        return None
    return secs[len(secs) // 2]
