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


# Chat auto-logging is stricter than /calories add: a bare number like "650"
# must never match (it would collide with lift posts), and bare "c" is too
# loose for free-form chat. The unit has to be spelled kcal/cal/kj. An
# A spelled-out unit (kcal/cal/cals/calories/kj/kilojoules) may carry an
# optional trailing note ("650kcal burrito"). A bare "c" is also accepted —
# people type "200 c" / "200.c" — but only when it's the whole message (no
# note), so casual text like "5 c u later" can't be mistaken for food.
# Either way it must be a single line so multi-line gym dumps fall through to
# the lift parser. The "[ \t.]*" separator lets "200c", "200 c" and "200.c"
# all work.
_CHAT_ENERGY_RE = re.compile(
    r"""
    ^\s*
    (?P<num>\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)
    [ \t.]*
    (?:
        (?P<unit>kcal|cals?|calories?|kj|kilojoules?)
        \b
        [ \t]*
        (?P<note>[^\n]{0,120})?
      |
        (?P<unit_c>c)
        [ \t]*
    )
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)


def parse_chat_message(text: str) -> tuple[float, str, str | None] | None:
    """If ``text`` is a bare calorie statement, return ``(kcal, unit, note)``.

    Returns ``None`` for anything else so the caller can fall through to
    the regular lift parser. ``unit`` is ``"kj"`` or ``"kcal"`` (normalised
    from what was typed); ``note`` is any trailing description, or None.
    """
    m = _CHAT_ENERGY_RE.match(text or "")
    if m is None:
        return None
    num = float(m.group("num").replace(",", ""))
    unit_raw = (m.group("unit") or m.group("unit_c") or "").lower()
    unit = "kj" if unit_raw.rstrip("s") in _KJ_UNITS else "kcal"
    kcal = kj_to_kcal(num) if unit == "kj" else num
    note = (m.group("note") or "").strip(" \t-–—:·") or None
    return kcal, unit, note


def normalize_food(name: str) -> str:
    """Canonical key for a saved food: lowercased, whitespace-collapsed."""
    return " ".join((name or "").strip().lower().split())


# A food shortcut phrase: an optional serving count (leading "2"/"2x" or
# trailing "x2") wrapped around a name. The name itself is matched loosely —
# the caller decides whether it's a *defined* food via a DB lookup, so this
# only needs to extract the count and the candidate name.
_FOOD_PHRASE_RE = re.compile(
    r"""
    ^\s*
    (?:(?P<lead>\d{1,3})\s*[x*×]?\s+)?
    (?P<name>.+?)
    (?:\s*[x*×]\s*(?P<trail>\d{1,3}))?
    \s*$
    """,
    re.VERBOSE,
)


def parse_food_phrase(text: str) -> tuple[int, str] | None:
    """Split a food shortcut into ``(servings, normalized_name)``.

    Handles ``coffee``, ``2 coffee``, ``2x coffee`` and ``coffee x2``.
    Returns None for multi-line or over-long text (never a food shortcut).
    Servings are clamped to 1..50. The name is *not* validated here — callers
    must confirm it's a saved food.
    """
    if not text or "\n" in text or len(text) > 64:
        return None
    m = _FOOD_PHRASE_RE.match(text)
    if m is None:  # pragma: no cover - the pattern matches any single line
        return None
    name = normalize_food(m.group("name"))
    if not name:
        return None
    qty_raw = m.group("lead") or m.group("trail")
    servings = int(qty_raw) if qty_raw else 1
    return max(1, min(servings, 50)), name


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
