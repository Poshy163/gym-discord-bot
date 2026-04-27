# Equipment name normalization.
# Maps many common aliases/spellings to a single canonical name so that
# e.g. "pec dec", "pec fly", "chest fly", "pekdek" all count as the same lift.

from __future__ import annotations

import re

# canonical_name -> list of aliases (lowercase, punctuation stripped)
_ALIAS_GROUPS: dict[str, list[str]] = {
    "bench press": [
        "bench press", "bench", "flat bench", "bench press 1rm", "bench max",
    ],
    "incline bench press": [
        "incline bench press", "incline bench", "incline press",
    ],
    "decline bench press": [
        "decline bench press", "decline bench", "decline press",
    ],
    "shoulder press": [
        "shoulder press", "ohp", "overhead press", "military press",
    ],
    "chest press": [
        "chest press", "machine chest press", "plate loaded chest press",
        "pin loaded chest press",
    ],
    "lat pulldown": [
        "lat pulldown", "lat pull down", "lat pull", "pulldown",
    ],
    "low row": ["low row", "seated row"],
    "back row": ["back row", "bent over row", "barbell row"],
    "tricep pushdown": [
        "tricep pushdown", "triceps pushdown", "tricep pull", "triceps pull",
        "tricep push down", "tricep extension", "triceps extension",
        "tricep extention", "triceps extention",
    ],
    "tricep overhead extension": [
        "tricep overhead extension", "tricep overhead extention",
        "overhead tricep extension", "overhead triceps extension",
    ],
    "rear delt fly": [
        "rear delt", "rear delt fly", "rear del", "reverse fly", "rear delt machine",
    ],
    "pec dec": [
        "pec dec", "pec fly", "pec deck", "pekdek", "chest fly", "chest flys",
        "chest flies",
    ],
    "solo arm pec dec": ["solo arm pec dec", "single arm pec dec"],
    "chin assist": ["chin assist", "assisted pullups", "assisted pull ups", "assisted pull-ups"],
    "pull ups": ["pull ups", "pullups", "pull-ups", "pull up"],
    "dips": ["dips", "dip"],
    "preacher curl": ["preacher curl", "preacher curls", "bicep preacher curls"],
    "hammer curl": ["hammer curl", "hammer curls", "bicep hammer curls"],
    "bicep curl": ["bicep curl", "biceps curl", "curl", "curls"],
    "lateral raise": [
        "lateral raise", "lateral raises", "lat raise", "lat raises",
        "lateral cable raises", "lateral cable raise",
    ],
    "wrist curl": ["wrist curl", "sam sulek wrist curl"],
    "reverse wrist curl": ["reverse wrist curl"],
    "cable crunch": ["cable crunch", "crunch", "crunches"],
    "dragon fly": ["dragon fly", "dragon flys", "dragon flies"],
    "leg raise": ["leg raise", "leg raises"],
    "leg press": [
        "leg press", "inclined leg press", "incline leg press",
        "angled leg press", "angle leg press", "hack squat leg press",
    ],
    "leg extension": ["leg extension", "leg extensions"],
    "leg curl": ["leg curl", "leg curls", "hamstring curl", "hamstring curls"],
    "hip adduction": ["hip adduction", "hip adductor", "adduction", "adductor"],
    "hip abduction": ["hip abduction", "hip abductor", "abduction", "abductor"],
    "calf raise": ["calf raise", "calf raises"],
    "squat": ["squat", "squats", "back squat"],
    "squat rack": ["squat rack", "squat racks", "power rack"],
    "diddy machine": ["diddy machine"],
    "deadlift": ["deadlift", "deadlifts", "dead lift"],
    "romanian deadlift": [
        "romanian deadlift", "romanian deadlifts", "rdl", "rdls",
        "stiff leg deadlift", "stiff legged deadlift",
    ],
    "push up": [
        "push up", "push ups", "pushup", "pushups", "push-up", "push-ups",
    ],
    "dumbbell press": [
        "dumbbell press", "db press", "dumbell press", "dumbbell bench",
        "dumbell bench", "db bench", "db bench press",
    ],
    "dumbbell shoulder press": [
        "dumbbell shoulder press", "db shoulder press",
        "dumbell shoulder press",
    ],
    "dumbbell": ["dumbbell", "dumbbells", "dumbell", "dumbells", "db"],
    "barbell": [
        "barbell", "barbells", "straight barbell", "straight barbells",
        "olympic barbell", "olympic bar",
    ],
    "ez bar": ["ez bar", "ez-bar", "ez barbell", "ez barbells", "curl bar"],
    "weight plate": ["weight plate", "weight plates"],
    "treadmill": ["treadmill", "treadmills"],
    "stairmaster": [
        "stairmaster", "stairmasters", "stair master", "stair masters",
        "stair climber",
    ],
    "spin bike": [
        "spin bike", "spin bikes", "exercise bike", "stationary bike",
    ],
    "assault bike": ["assault bike", "assault bikes", "air bike"],
    "elliptical": ["elliptical", "ellipticals", "cross trainer"],
    "rowing machine": [
        "rowing machine", "rowing machines", "rower", "erg", "ergometer",
    ],
    "plyo box": ["plyo box", "plyo boxes", "box jump", "box jumps"],
    "resistance band": ["resistance band", "resistance bands"],
    "kettlebell": ["kettlebell", "kettlebells", "kb"],
    "slam ball": ["slam ball", "slam balls"],
    "medicine ball": ["medicine ball", "medicine balls", "med ball", "med balls"],
    "sled": ["sled", "sleds", "push sled", "sled push", "prowler", "prowler sled"],
}


def _normalize_token(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9 +]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# Public re-export so other modules don't need to reach into the underscore
# name. The underscore form is kept for backwards compat with existing imports.
normalize_token = _normalize_token


# Build reverse lookup (alias -> canonical)
_ALIAS_TO_CANON: dict[str, str] = {}
for canon, aliases in _ALIAS_GROUPS.items():
    _ALIAS_TO_CANON[_normalize_token(canon)] = canon
    for a in aliases:
        _ALIAS_TO_CANON[_normalize_token(a)] = canon


def canonicalize(name: str) -> str:
    """Return the canonical equipment name for an input label.

    If the label is unknown, a cleaned-up version of the input is returned
    so it is still stored consistently.
    """
    key = _normalize_token(name)
    if not key:
        return ""
    if key in _ALIAS_TO_CANON:
        return _ALIAS_TO_CANON[key]
    # Try dropping trailing plural "s"
    if key.endswith("s") and key[:-1] in _ALIAS_TO_CANON:
        return _ALIAS_TO_CANON[key[:-1]]
    # Handle apostrophe-s artefacts like "dragon fly's" -> "dragon fly s"
    if key.endswith(" s") and key[:-2] in _ALIAS_TO_CANON:
        return _ALIAS_TO_CANON[key[:-2]]
    return key


def aliases_for(canonical: str) -> list[str]:
    """Return the list of aliases known for a canonical equipment name.
    Returns an empty list if the canonical name isn't in the table."""
    return list(_ALIAS_GROUPS.get(canonical, []))


def all_canonicals() -> list[str]:
    """Return the full list of canonical equipment names known to the bot."""
    return list(_ALIAS_GROUPS.keys())
