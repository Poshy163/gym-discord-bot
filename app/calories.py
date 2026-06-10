"""Calorie/kilojoule parsing and conversion helpers.

Everything here is pure and Discord-free so it can be unit-tested directly.
The bot stores energy internally in **kcal** ("calories" in everyday speech;
Australian food labels print kJ, hence the converter).
"""
from __future__ import annotations

import re

# Thermochemical-ish food-label constant: 1 kcal = 4.184 kJ.
KJ_PER_KCAL = 4.184


def kj_to_kcal(kj: float) -> float:
    return kj / KJ_PER_KCAL


def kcal_to_kj(kcal: float) -> float:
    return kcal * KJ_PER_KCAL


# Accepts "850", "850c", "850 cal", "850kcal", "850 calories", "3,550kJ",
# "3550 kj", "2 100 kilojoules". Bare numbers default to kcal — that's what
# people mean when they say "I had 600".
_ENERGY_RE = re.compile(
    r"""
    ^\s*
    (?P<num>\d{1,3}(?:[ ,]\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)
    \s*
    (?P<unit>kj|kilojoules?|kcal|cals?|calories?|c)?
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

_KJ_UNITS = {"kj", "kilojoule"}


def parse_energy(text: str) -> tuple[float, str] | None:
    """Parse a free-form energy amount into ``(kcal, unit_entered)``.

    ``unit_entered`` is ``"kj"`` or ``"kcal"`` (what the user typed, so the
    reply can echo the conversion). Returns None when the text isn't an
    energy amount. Negative amounts aren't representable by the grammar —
    corrections go through the undo path instead.
    """
    m = _ENERGY_RE.match(text or "")
    if m is None:
        return None
    num = float(m.group("num").replace(",", "").replace(" ", ""))
    unit_raw = (m.group("unit") or "").lower()
    unit = unit_raw.rstrip("s")
    if unit in _KJ_UNITS:
        return kj_to_kcal(num), "kj"
    return num, "kcal"


def format_kcal(kcal: float) -> str:
    """Render a kcal amount the way the bot displays it (whole numbers)."""
    return f"{round(kcal):,} cal"


def progress_bar(current: float, target: float, width: int = 12) -> str:
    """Text progress bar for today's intake vs the daily target.

    Overshoot is clamped to a full bar; the percentage next to it tells the
    real story.
    """
    if target <= 0:
        return "·" * width
    frac = max(0.0, min(1.0, current / target))
    filled = int(round(frac * width))
    return "█" * filled + "░" * (width - filled)
