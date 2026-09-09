"""Recipe -> danbooru-tag prompt compiler.

Illustrious/NoobAI-family checkpoints are trained on danbooru tags, so they
already know the characters by name. Fusion is therefore a tag-blending
problem, not a fine-tuning one - the job here is to phrase the blend so the
sampler commits to one coherent design instead of averaging two into mush.
"""

# Illustrious-family models respond hard to these quality preambles; without
# them output skews toward low-effort booru fanart.
QUALITY = "masterpiece, best quality, amazing quality, very aesthetic, absurdres, highres"

DEFAULT_NEGATIVE = (
    "bad quality, worst quality, worst detail, low quality, lowres, sketch, "
    "jpeg artifacts, watermark, signature, text, logo, username, "
    "bad hands, bad anatomy, extra digits, fewer digits, missing fingers, "
    "multiple views, censored"
)

# Each mode is a different answer to "what does mixing two franchises mean?".
# The templates are deliberately opinionated - vague blends render as mush.
MODES = {
    "design_fusion": {
        "label": "Design fusion",
        "hint": "One character redesigned in the other's visual language. The workhorse mode.",
        "template": (
            "1girl, solo, original character, "
            "{a} redesigned as {b}, character design fusion, "
            "wearing {b} styled outfit, {b} colour palette, "
            "full body, standing, simple background, character sheet lighting"
        ),
    },
    "crossover_scene": {
        "label": "Crossover scene",
        "hint": "Both characters together in one shot. Best for narrative posts.",
        "template": (
            "2characters, {a} and {b}, crossover, "
            "dynamic composition, facing viewer, dramatic lighting, "
            "detailed background, cinematic"
        ),
    },
    "style_transfer": {
        "label": "World transplant",
        "hint": "Subject A rendered inside B's world and art direction.",
        "template": (
            "{a}, drawn in the world of {b}, "
            "{b} art style, {b} atmosphere and colour grading, "
            "upper body, detailed background, cinematic lighting"
        ),
    },
    "original_oc": {
        "label": "Original character",
        "hint": "A new character carrying DNA from both. Most 'original' output.",
        "template": (
            "1girl, solo, original character, completely original design, "
            "design elements inspired by {a}, motifs inspired by {b}, "
            "unique outfit, full body, standing, simple background"
        ),
    },
}

# Curated starting points so the first session isn't a blank page.
STARTERS = [
    {"name": "Gardevoir x Gojo",      "subject_a": "gardevoir",  "subject_b": "gojo satoru",  "mode": "design_fusion",
     "extra": "blindfold, white hair, cursed energy, glowing blue eyes"},
    {"name": "Lucario x Sukuna",      "subject_a": "lucario",    "subject_b": "ryomen sukuna", "mode": "design_fusion",
     "extra": "facial markings, four arms, menacing grin, red and black palette"},
    {"name": "Pikachu in Shibuya",    "subject_a": "pikachu",    "subject_b": "jujutsu kaisen", "mode": "style_transfer",
     "extra": "night city, neon, cursed energy crackling, rain"},
    {"name": "Gengar curse spirit",   "subject_a": "gengar",     "subject_b": "cursed spirit", "mode": "design_fusion",
     "extra": "eldritch, purple mist, unsettling smile, horror atmosphere"},
    {"name": "Nobara x Sylveon",      "subject_a": "kugisaki nobara", "subject_b": "sylveon", "mode": "design_fusion",
     "extra": "pink ribbons, hammer, confident pose"},
]


def compile_prompt(subject_a: str, subject_b: str, mode: str, extra: str = "") -> str:
    """Build the positive prompt. Order matters: SDXL weights earlier tags more."""
    spec = MODES.get(mode) or MODES["design_fusion"]
    body = spec["template"].format(a=subject_a.strip(), b=subject_b.strip())
    parts = [QUALITY, body]
    if extra.strip():
        parts.append(extra.strip())
    return ", ".join(p for p in parts if p)


def compile_negative(extra: str = "") -> str:
    if extra.strip():
        return f"{DEFAULT_NEGATIVE}, {extra.strip()}"
    return DEFAULT_NEGATIVE
