"""Calorie/kilojoule parsing and conversion helpers.

Everything here is pure and Discord-free so it can be unit-tested directly.
The bot stores energy internally in **kcal** ("calories" in everyday speech;
Australian food labels print kJ, hence the converter).
"""
from __future__ import annotations

import re
from typing import NamedTuple

from . import scaling

# Thermochemical-ish food-label constant: 1 kcal = 4.184 kJ.
KJ_PER_KCAL = 4.184


def kj_to_kcal(kj: float) -> float:
    return kj / KJ_PER_KCAL


def kcal_to_kj(kcal: float) -> float:
    return kcal * KJ_PER_KCAL


# Optional multiplier prefix for label maths: food labels list energy per
# 100 g, so eating 70 g of a "1640 kJ per 100 g" food is `0.7x1640kj`. The
# multiplier scales the amount (0.7 × 1640 = 1148 kJ). Accepts x / * / ×.
# `app.scaling` adds the friendlier way to say the same thing — state the
# serving once, as `1640kj 70g` or `1640kj x0.7` — but this prefix stays.
_MULT = r"(?:(?P<mult>\d+(?:\.\d+)?|\.\d+)\s*[x*×]\s*)?"

# Accepts "850", "850c", "850 cal", "850kcal", "850 calories", "3,550kJ",
# "3550 kj", "2 100 kilojoules", "0.7x1640kj". Bare numbers default to kcal —
# that's what people mean when they say "I had 600".
_ENERGY_RE = re.compile(
    rf"""
    ^\s*
    {_MULT}
    (?P<num>\d{{1,3}}(?:[ ,]\d{{3}})*(?:\.\d+)?|\d+(?:\.\d+)?)
    \s*
    (?P<unit>kj|kilojoules?|kcal|cals?|calories?|c)?
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

_KJ_UNITS = {"kj", "kilojoule"}


def match_energy(text: str) -> tuple[float, str] | None:
    """Parse an energy amount **exactly as typed**, applying no message-wide
    scale. See :func:`parse_energy` for the whole story; this is the half that
    :mod:`app.scaling` retries against a scale-stripped string."""
    m = _ENERGY_RE.match(text or "")
    if m is None:
        return None
    num = float(m.group("num").replace(",", "").replace(" ", ""))
    if m.group("mult"):
        num *= float(m.group("mult"))
    unit_raw = (m.group("unit") or "").lower()
    unit = unit_raw.rstrip("s")
    if unit in _KJ_UNITS:
        return kj_to_kcal(num), "kj"
    return num, "kcal"


def parse_energy(text: str) -> tuple[float, str] | None:
    """Parse a free-form energy amount into ``(kcal, unit_entered)``.

    ``unit_entered`` is ``"kj"`` or ``"kcal"`` (what the user typed, so the
    reply can echo the conversion). Per-100g label maths is supported two
    ways: a multiplier prefix on the number (``0.7x1640kj``), or a
    message-wide scale token stating the serving once (``1640kj 70g``,
    ``1640kj x0.7``) — see :mod:`app.scaling`. Returns None when the text
    isn't an energy amount. Negative amounts aren't representable by the
    grammar — corrections go through the undo path instead.
    """
    resolved = scaling.resolve(text, match_energy)
    if resolved is None:
        return None
    (kcal, unit), scale = resolved
    return scaling.apply(kcal, scale), unit


# Chat auto-logging is deliberately strict: the message must be ONLY the
# amount — a number plus a unit (kcal/cal/cals/calories/kj/kilojoules, or a
# bare "c") and nothing else but trailing whitespace/punctuation. That rejects
# sentences like "1500cal is crazy work" so casual chatter is never logged;
# descriptions go through "/calories add ... note" instead. A bare number
# never matches (it would collide with lift posts). The "[ \t.]*" separator
# lets "200c", "200 c" and "200.c" all work. A multiplier prefix scales the
# amount for per-100g label maths: "0.7x1640kj" logs 0.7 × 1640 kJ.
_CHAT_ENERGY_RE = re.compile(
    rf"""
    ^\s*
    {_MULT}
    (?P<num>\d{{1,3}}(?:,\d{{3}})*(?:\.\d+)?|\d+(?:\.\d+)?)
    [ \t.]*
    (?P<unit>kcal|cals?|calories?|kj|kilojoules?|c)
    [\s.!?]*$
    """,
    re.IGNORECASE | re.VERBOSE,
)


def match_chat_energy(text: str) -> tuple[float, str, str | None] | None:
    """Match a chat calorie amount **exactly as typed**, applying no
    message-wide scale. The scaled half is :func:`parse_chat_message`."""
    m = _CHAT_ENERGY_RE.match(text or "")
    if m is None:
        return None
    num = float(m.group("num").replace(",", ""))
    if m.group("mult"):
        num *= float(m.group("mult"))
    unit_raw = m.group("unit").lower()
    unit = "kj" if unit_raw.rstrip("s") in _KJ_UNITS else "kcal"
    kcal = kj_to_kcal(num) if unit == "kj" else num
    return kcal, unit, None


def parse_chat_message(text: str) -> tuple[float, str, str | None] | None:
    """If ``text`` is *only* a calorie amount, return ``(kcal, unit, None)``.

    Returns ``None`` for anything else (including amounts buried in a
    sentence) so the caller can fall through to the regular lift parser.
    ``unit`` is ``"kj"`` or ``"kcal"`` (normalised from what was typed). The
    third element is always ``None`` — chat posts don't carry notes; use
    ``/calories add`` for those.

    A trailing scale token logs a per-100g label straight off the packet:
    ``895kj 110g`` is 110 g of an 895 kJ/100 g food, and ``895kj x1.1`` says
    the same thing as a multiplier. See :mod:`app.scaling`.
    """
    resolved = scaling.resolve(text, match_chat_energy)
    if resolved is None:
        return None
    (kcal, unit, note), scale = resolved
    return scaling.apply(kcal, scale), unit, note


def normalize_food(name: str) -> str:
    """Canonical key for a saved food: lowercased, whitespace-collapsed."""
    return " ".join((name or "").strip().lower().split())


# A food shortcut phrase: an optional serving count (leading "2"/"2x" or
# trailing "x2") wrapped around a name. The name itself is matched loosely —
# the caller decides whether it's a *defined* food via a DB lookup, so this
# only needs to extract the count and the candidate name.
#
# Counts may be fractional (``0.5 milk``, ``.5 milk``, ``milk x1.5``): half a
# serving is an ordinary thing to eat, and without it the only way to log one
# was to do the arithmetic by hand and type the kcal.
_QTY = r"\d{1,3}(?:\.\d{1,3})?|\.\d{1,3}"
_FOOD_PHRASE_RE = re.compile(
    rf"""
    ^\s*
    (?:(?P<lead>{_QTY})\s*[x*×]?\s+)?
    (?P<name>.+?)
    (?:\s*[x*×]\s*(?P<trail>{_QTY}))?
    \s*$
    """,
    re.VERBOSE,
)

# Ceiling on one shortcut's servings — anything beyond this is a typo, not a
# meal. There's no floor beyond "more than nothing": a count that rounds away
# to zero is rejected outright rather than quietly logged as one serving.
_MAX_SERVINGS = 50.0
_SERVING_DECIMALS = 3


def parse_food_phrase(text: str) -> tuple[float, str] | None:
    """Split a food shortcut into ``(servings, normalized_name)``.

    Handles ``coffee``, ``2 coffee``, ``2x coffee``, ``coffee x2`` and
    fractional counts (``0.5 milk``, ``milk x1.5``). Returns None for
    multi-line or over-long text (never a food shortcut), and for a count of
    zero — ``0 coffee`` is not one coffee. Servings are capped at 50 and
    rounded to 3 decimals. The name is *not* validated here — callers must
    confirm it's a saved food.
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
    if qty_raw is None:
        return 1.0, name
    servings = round(float(qty_raw), _SERVING_DECIMALS)
    if servings <= 0:
        return None
    return min(servings, _MAX_SERVINGS), name


def format_servings(servings: float) -> str:
    """Render a serving count for display: ``2``, ``0.5``, ``1.25``.

    Whole counts lose the decimal point — a saved food logged twice should read
    ``coffee ×2``, not ``coffee ×2.0``.
    """
    n = float(servings)
    return str(int(n)) if n.is_integer() else f"{n:g}"


# ---------------------------------------------------------------------------
# Reading a stored note back into the food it came from
# ---------------------------------------------------------------------------

# ``calorie_entries`` has no food id — the only link between an entry and what
# was eaten is the free-text ``note`` the logger wrote. Tallying per food means
# reading that link back out. The shapes that carry an identity, all built in
# app/bot.py:
#
#   saved food, one serving   "Flat White"
#   saved food, N servings    "Flat White ×2"
#   saved meal                "Breakfast (meal)"
#   AI estimate               "Big Mac Meal (AI estimate)", "Weet-Bix (110 g, label)"
#
# and one that does not: the scaling footnote the bot adds to a plain
# ``895kj 110g`` post ("scaled ×1.1", "110 g of the per-100 g values"). That is
# the bot's own arithmetic showing its work, not a thing anyone ate, so it is
# dropped — and dropped *before* the serving suffix is read, because
# "scaled ×1.1" would otherwise tally as 1.1 servings of a food called "scaled".

#: Suffixes bot.py appends to mark what produced an entry.
MEAL_SUFFIX = " (meal)"
AI_SUFFIX = " (AI estimate)"

#: What bot.py writes in place of a name the model didn't supply — "label" for
#: an unreadable nutrition panel, "AI estimate" for an unnamed guess. They name
#: no food, and treating them as one fuses unrelated meals into a single row
#: called "label". Counted as unnamed instead.
_AI_PLACEHOLDERS = {"label", "ai estimate"}

#: Grams are unbounded here (a label serving can exceed 999), so this is looser
#: than ``_QTY`` — it only has to recognise text the bot itself wrote.
_NUM = r"\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?"

_SCALE_NOTE_RE = re.compile(
    rf"^(?:scaled\s*[x*×]\s*(?:{_NUM})"
    rf"|(?:{_NUM})\s*g of the per-100 g values)$",
    re.IGNORECASE,
)
_AI_LABEL_RE = re.compile(
    rf"\s*\((?:{_NUM})\s*g,\s*label\)$", re.IGNORECASE,
)
# The written form is always U+00D7, but x and * are accepted for the same
# reason ``_FOOD_PHRASE_RE`` accepts them: a hand-typed note should read too.
# The lookbehind is what keeps that lenience honest — without it the final
# letter of "trail mix 2" or "Twix 2" reads as the sign, and the tally grows a
# food called "trail mi" that was apparently had twice.
_NOTE_SERVINGS_RE = re.compile(
    rf"(?<![^\W\d_])\s*[x*×]\s*(?P<n>{_QTY})$",
)


class EntryLabel(NamedTuple):
    """What one stored note says was eaten.

    ``key`` is the :func:`normalize_food` grouping key — entries are folded on
    it, never on the raw note, so ``"Coffee"`` and ``"coffee ×2"`` land in the
    same bucket. ``display`` keeps the casing as it was logged, which is what a
    food renamed since is still labelled with when nothing better is known.
    """

    key: str
    display: str
    servings: float
    kind: str


def parse_entry_note(note: str | None) -> EntryLabel | None:
    """Read a ``calorie_entries.note`` back into the food it identifies.

    Returns None when the note names nothing edible — it is empty, or it is
    one of the bot's own scaling footnotes. ``kind`` is ``"food"``,
    ``"meal"`` or ``"ai"``; only a plain saved food carries a serving count,
    since meals are always logged whole and an AI label is model prose where a
    trailing "x2" would be words rather than a quantity.
    """
    if not note:
        return None
    text = " ".join(note.split())
    if not text or _SCALE_NOTE_RE.match(text):
        return None

    kind = "food"
    lowered = text.lower()
    if lowered.endswith(MEAL_SUFFIX):
        kind, text = "meal", text[: -len(MEAL_SUFFIX)].rstrip()
    elif lowered.endswith(AI_SUFFIX.lower()):
        kind, text = "ai", text[: -len(AI_SUFFIX)].rstrip()
    else:
        without = _AI_LABEL_RE.sub("", text)
        if without != text:
            kind, text = "ai", without.rstrip()

    servings = 1.0
    if kind == "food":
        m = _NOTE_SERVINGS_RE.search(text)
        # A note that is *only* a count ("×2") has no food in it to strip the
        # count off, so it is left whole rather than reduced to nothing.
        if m is not None and text[: m.start()].rstrip():
            servings = round(float(m.group("n")), _SERVING_DECIMALS)
            text = text[: m.start()].rstrip()

    key = normalize_food(text)
    if not key or servings <= 0:
        return None
    if kind == "ai" and key in _AI_PLACEHOLDERS:
        return None
    return EntryLabel(key, text, servings, kind)


# Meals: a named bundle of saved foods ("breakfast" = coffee + oats + shake)
# logged in one go. Items are entered as a comma/plus-separated list where
# each piece uses the same serving syntax as a food shortcut ("2x oats").
_MEAL_MAX_ITEMS = 12


def parse_meal_items(text: str) -> list[tuple[float, str]] | None:
    """Split a meal definition into ``[(servings, normalized_name), ...]``.

    Accepts "coffee, 2x oats, protein shake" or "coffee + oats", with the same
    fractional counts as a food shortcut ("0.5 milk"). Names are normalized but
    NOT validated — callers must check each against the user's saved foods.
    Returns None for empty input or over 12 items. Duplicate names merge by
    summing servings so "coffee, coffee" is ``[(2, "coffee")]``.
    """
    if not text or "\n" in text:
        return None
    merged: dict[str, float] = {}
    for piece in re.split(r"[,+]", text):
        piece = piece.strip()
        if not piece:
            continue
        parsed = parse_food_phrase(piece)
        if parsed is None:
            return None
        servings, name = parsed
        merged[name] = min(
            round(merged.get(name, 0.0) + servings, _SERVING_DECIMALS),
            _MAX_SERVINGS,
        )
    if not merged or len(merged) > _MEAL_MAX_ITEMS:
        return None
    return [(count, name) for name, count in merged.items()]


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
