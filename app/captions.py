"""Caption + hashtag drafting via Ollama on the same desktop.

Deliberately the same box as the renderer: captions inherit the render node's
availability envelope instead of adding a second thing that can be down.
"""
import json
import re

import httpx

from .config import OLLAMA_URL, OLLAMA_MODEL

SYSTEM = (
    "You write Instagram captions for an anime fan-art account that posts "
    "original character mashups. Voice: confident, a little playful, never "
    "cringe. No emoji spam - two at most. Never use the words 'dive', "
    "'unleash', 'elevate', or 'game-changer'. Do not mention AI."
)

TEMPLATE = """This post is an original mashup illustration.

Source A: {a}
Source B: {b}
Blend: {mode}
Extra direction: {extra}

Write:
1. A caption of 1-2 sentences that names the mashup and gives it some attitude.
2. Exactly 12 hashtags - a mix of broad reach and niche fandom tags.

Respond ONLY as JSON: {{"caption": "...", "hashtags": ["#tag", ...]}}"""


async def draft(subject_a: str, subject_b: str, mode: str, extra: str = "") -> dict:
    prompt = TEMPLATE.format(a=subject_a, b=subject_b, mode=mode, extra=extra or "none")
    try:
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(
                f"{OLLAMA_URL}/api/chat",
                json={
                    "model": OLLAMA_MODEL,
                    "messages": [
                        {"role": "system", "content": SYSTEM},
                        {"role": "user", "content": prompt},
                    ],
                    "stream": False,
                    "format": "json",
                    "options": {"temperature": 0.8},
                },
            )
            r.raise_for_status()
            content = r.json()["message"]["content"]
    except Exception as e:
        return {"ok": False, "error": f"Ollama unreachable ({e}). Desktop asleep?"}

    data = _parse(content)
    if data is None:
        return {"ok": False, "error": "model did not return usable JSON"}

    tags = [t if t.startswith("#") else f"#{t}" for t in data.get("hashtags", [])]
    return {"ok": True, "caption": data.get("caption", "").strip(), "hashtags": tags}


def _parse(content: str):
    try:
        return json.loads(content)
    except ValueError:
        pass
    # format:json usually holds, but a stray code fence is cheap to recover from.
    m = re.search(r"\{.*\}", content, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None
