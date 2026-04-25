# Parse gym-post messages into structured lift entries.
#
# Handles lines like:
#   "Shoulder press: 31kg"
#   "Bench Press 1RM: 100kg"
#   "Incline bench 70"
#   "Legs 3.5 plates chill"
#   "Dips: BW+20kg"
#   "Leg curls: 50 - 77 kg"
#   "Pec fly: 45kg L and R"
#   "Squats: 60 kg"
#   "Bench: 2x20 + 10 kg" (plate math)
#   "Bench: 100kg x5" (captures reps)

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from .aliases import canonicalize, normalize_token

# Set of all canonical equipment names known to the alias table. Used to
# gate "label number" lines where no weight unit is given.
from .aliases import _ALIAS_GROUPS as _AG  # noqa: PLC2701  (internal use)
_KNOWN_CANONICALS: set[str] = set(_AG.keys())

# All alias phrases (canonical + aliases) as lowercase strings, sorted longest
# first so "leg press" matches before "press" when scanning free-form text.
_BUILTIN_ALIAS_PHRASES: list[str] = sorted(
    {p.lower() for canon, aliases in _AG.items() for p in (canon, *aliases)},
    key=lambda s: (-len(s), s),
)
_BUILTIN_FREEFORM_RE = re.compile(
    r"\b(" + "|".join(re.escape(p) for p in _BUILTIN_ALIAS_PHRASES) + r")\b",
    re.IGNORECASE,
)

# Assumed plate weight in kg (standard Olympic plate per side). Override with
# the PLATE_KG env var for gyms with non-standard plates (15kg / 25lb / etc.).
try:
    PLATE_KG = float(os.getenv("PLATE_KG", "20"))
except ValueError:
    PLATE_KG = 20.0

# Section headers we want to ignore as exercise names.
_SECTION_HEADERS = {
    "chest", "back", "arms", "legs", "core", "other", "shoulders", "biceps",
    "triceps",
}

# Lines that contain these tokens are treated as non-lift chatter and skipped
# even if they look numeric (e.g. "BW (Body Weight) - 67kg").
_SKIP_LINE_TOKENS = ("body weight", "bodyweight")

_NUM = r"\d+(?:\.\d+)?"

# Matches "equipment: value" style lines.
_COLON_RE = re.compile(r"^\s*([A-Za-z][A-Za-z '\-/]{1,60}?)\s*[:\-]\s*(.+?)\s*$")

# Matches a value portion and pulls out a weight.
#   "45kg", "45 kg", "BW+20kg", "6 plates", "3.5 plates",
#   "50 - 77 kg", "45kg L and R", "80kg", "BW"
_RANGE_RE = re.compile(rf"({_NUM})\s*-\s*({_NUM})\s*kg?", re.IGNORECASE)
_BW_PLUS_RE = re.compile(rf"bw\s*\+\s*({_NUM})\s*kg?", re.IGNORECASE)
_PLATES_RE = re.compile(rf"({_NUM})\s*plates?", re.IGNORECASE)
_KG_RE = re.compile(rf"({_NUM})\s*kg", re.IGNORECASE)
_BARE_NUM_RE = re.compile(rf"(?<![\w.])({_NUM})(?!\s*(?:rm|rep|reps|set|sets))", re.IGNORECASE)
_BW_RE = re.compile(r"\bbw\b", re.IGNORECASE)

# Captures rep counts written as "x5", "x 8", "for 6 reps", "10 reps", etc.
# We deliberately ignore "1RM" (already handled as max-effort context).
_REPS_RE = re.compile(
    rf"(?:x\s*({_NUM})|for\s+({_NUM})\s*reps?|({_NUM})\s*reps?\b)",
    re.IGNORECASE,
)

# Plate math like "2x20 + 10" or "20+20+10" — useful when people describe
# what's actually loaded on the bar instead of a single total. Only applied
# when the expression is followed by an explicit "kg" unit so we don't
# misinterpret rep schemes like "5x5" as a weight.
_PLATE_EXPR_RE = re.compile(
    rf"((?:(?:{_NUM})\s*(?:[x*]\s*{_NUM})?(?:\s*\+\s*(?:{_NUM})\s*(?:[x*]\s*{_NUM})?)+)\s*)kg",
    re.IGNORECASE,
)

# Matches headings like "April" or "May 2026" - skip these as lines
_MONTH_HEADING_RE = re.compile(
    r"^\s*(january|february|march|april|may|june|july|august|september|october|"
    r"november|december)(\s+\d{4})?\s*$",
    re.IGNORECASE,
)


@dataclass
class Lift:
    equipment: str   # canonical name
    weight_kg: float
    bodyweight_add: bool = False   # True if weight is added on top of bodyweight
    raw: str = ""                  # original line for reference
    # True when the weight was extracted with an explicit unit (kg / plates /
    # BW+) rather than a bare number. Used to decide whether a single-line
    # message is confident enough to auto-store.
    confident: bool = False
    # Rep count when the message included one (e.g. "100kg x5"). None when
    # the user didn't say. Pure metadata — the bot stores it for future
    # 1RM estimates and never blocks on its absence.
    reps: int | None = None


def _eval_plate_expr(expr: str) -> float | None:
    """Sum a plate-loading expression like '2x20 + 10' into kilograms.

    Multiplication binds tighter than addition (because plates load in
    multiples and you'd never write '2x20+10' to mean 2*(20+10)). Returns
    None if anything fails to parse cleanly.
    """
    expr = expr.strip()
    if not expr:
        return None
    total = 0.0
    for term in expr.split("+"):
        term = term.strip()
        if not term:
            return None
        # Allow either 'x' or '*' as the multiplier symbol.
        m = re.fullmatch(rf"({_NUM})\s*[x*]\s*({_NUM})", term, re.IGNORECASE)
        if m:
            total += float(m.group(1)) * float(m.group(2))
            continue
        try:
            total += float(term)
        except ValueError:
            return None
    return total if total > 0 else None


def _extract_reps(value: str) -> int | None:
    """Return a sensible rep count from the value text, or None.

    Rejects suspiciously large numbers (>50) — those are almost certainly
    weights or set/rep totals being misread, not single-set rep counts.
    """
    m = _REPS_RE.search(value)
    if not m:
        return None
    raw = m.group(1) or m.group(2) or m.group(3)
    try:
        n = int(float(raw))
    except (TypeError, ValueError):
        return None
    if n <= 0 or n > 50:
        return None
    return n


def _extract_weight(value: str) -> tuple[float | None, bool, bool]:
    """Extract (weight_kg, bodyweight_add_flag, confident) from the RHS text.

    ``confident`` is True when the input used an explicit unit (kg / plates /
    BW / BW+), False for bare numbers with no unit.
    Returns (None, False, False) if no usable number is found.
    """
    v = value.strip()
    if not v:
        return None, False, False

    # BW+20kg
    m = _BW_PLUS_RE.search(v)
    if m:
        return float(m.group(1)), True, True

    # "50 - 77 kg" range -> take the higher end (represents top working weight)
    m = _RANGE_RE.search(v)
    if m:
        return max(float(m.group(1)), float(m.group(2))), False, True

    # "6 plates" / "3.5 plates"
    m = _PLATES_RE.search(v)
    if m:
        return float(m.group(1)) * PLATE_KG, False, True

    # Plate math like "2x20 + 10kg" — must be followed by 'kg' to avoid
    # eating rep schemes ("5x5"). Try this before the plain `_KG_RE` so we
    # capture the full sum rather than just the trailing number.
    m = _PLATE_EXPR_RE.search(v)
    if m:
        total = _eval_plate_expr(m.group(1))
        if total is not None:
            return total, False, True

    # explicit "Xkg"
    m = _KG_RE.search(v)
    if m:
        return float(m.group(1)), False, True

    # "BW" alone -> bodyweight, weight 0
    if _BW_RE.search(v) and not _BARE_NUM_RE.search(v):
        return 0.0, True, True

    # bare number with no unit (e.g. "Incline bench 70")
    m = _BARE_NUM_RE.search(v)
    if m:
        n = float(m.group(1))
        # Sanity filter: ignore tiny numbers that are likely rep counts.
        if n >= 5:
            return n, False, False

    return None, False, False


def _looks_like_equipment(label: str) -> bool:
    key = label.strip().lower()
    if not key:
        return False
    if key in _SECTION_HEADERS:
        return False
    if _MONTH_HEADING_RE.match(key):
        return False
    # require at least one letter
    if not re.search(r"[a-z]", key):
        return False
    return True


def _build_freeform_re(
    custom_aliases: dict[str, str] | None,
) -> tuple[re.Pattern[str], dict[str, str]]:
    """Return a regex matching either built-in alias phrases or any custom
    aliases supplied by the caller, plus a normalised lookup table.

    Custom aliases are stored normalised (whitespace/punctuation collapsed),
    but we have to match the *original* phrasing in user text. We expand
    each normalised key by inserting flexible whitespace so 'hack sled'
    matches 'hack  sled' too.
    """
    if not custom_aliases:
        return _BUILTIN_FREEFORM_RE, {}
    extra: dict[str, str] = {}
    extra_phrases: list[str] = []
    for norm, canon in custom_aliases.items():
        if not norm:
            continue
        extra[norm] = canon
        # Allow runs of whitespace between tokens.
        pattern = r"\s+".join(re.escape(t) for t in norm.split())
        extra_phrases.append(pattern)
    if not extra_phrases:
        return _BUILTIN_FREEFORM_RE, {}
    builtin_phrases = [re.escape(p) for p in _BUILTIN_ALIAS_PHRASES]
    all_phrases = sorted(
        builtin_phrases + extra_phrases,
        key=lambda s: (-len(s), s),
    )
    pattern = re.compile(r"\b(" + "|".join(all_phrases) + r")\b", re.IGNORECASE)
    return pattern, extra


def _resolve_with_custom(
    name: str, custom_aliases: dict[str, str] | None,
) -> str:
    """Canonicalize a label, consulting custom aliases first."""
    if custom_aliases:
        key = normalize_token(name)
        if key and key in custom_aliases:
            return custom_aliases[key]
    return canonicalize(name)


def _freeform_match(
    line: str,
    freeform_re: re.Pattern[str],
    custom_aliases: dict[str, str],
) -> tuple[str, float, bool, int | None] | None:
    """Scan a free-form sentence for (equipment, weight_kg, bodyweight_add, reps).

    Returns None unless the line contains both a known equipment phrase
    (built-in or custom-aliased) and a weight carrying an explicit unit.
    Avoids false positives from casual chat.
    """
    m = freeform_re.search(line)
    if not m:
        return None
    canon = _resolve_with_custom(m.group(1), custom_aliases)
    if canon not in _KNOWN_CANONICALS and canon not in custom_aliases.values():
        return None

    reps = _extract_reps(line)

    # Only accept weights with an explicit unit in free-form — bare numbers
    # are too ambiguous in a sentence.
    bw_plus = _BW_PLUS_RE.search(line)
    if bw_plus:
        return canon, float(bw_plus.group(1)), True, reps
    rng = _RANGE_RE.search(line)
    if rng:
        return canon, max(float(rng.group(1)), float(rng.group(2))), False, reps
    plates = _PLATES_RE.search(line)
    if plates:
        return canon, float(plates.group(1)) * PLATE_KG, False, reps
    expr = _PLATE_EXPR_RE.search(line)
    if expr:
        total = _eval_plate_expr(expr.group(1))
        if total is not None:
            return canon, total, False, reps
    kg = _KG_RE.search(line)
    if kg:
        return canon, float(kg.group(1)), False, reps
    return None


def parse_message(
    text: str,
    custom_aliases: dict[str, str] | None = None,
) -> list[Lift]:
    """Parse a whole message and return any lifts detected.

    ``custom_aliases`` is an optional ``{normalized_phrase: canonical}``
    mapping (typically loaded from the per-guild custom_aliases table) that
    extends the built-in alias set. When provided, free-form parsing and
    label canonicalization both consult it first.
    """
    custom_aliases = custom_aliases or {}
    freeform_re, _ = _build_freeform_re(custom_aliases)
    lifts: list[Lift] = []
    seen: set[str] = set()  # canonical equipment names in this message

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lower = line.lower()
        if any(tok in lower for tok in _SKIP_LINE_TOKENS):
            continue
        if _MONTH_HEADING_RE.match(line):
            continue
        if lower in _SECTION_HEADERS:
            continue

        label: str | None = None
        value: str | None = None
        # For non-colon lines we always require the label to be a known
        # exercise. Without a colon, there's no reliable cue where the label
        # ends, so "i did 46kg on the leg curl" would otherwise be stored as
        # equipment="i did". Colon-separated lines remain free-form because the
        # writer explicitly said "<thing>: <value>".
        require_known = False

        m = _COLON_RE.match(line)
        if m:
            label = m.group(1).strip()
            value = m.group(2).strip()
        else:
            # "equipment number..." — capture everything up to the first digit
            # as the label so multi-word names ("Calf Raises", "Chest Fly") work.
            m2 = re.match(r"^\s*([A-Za-z][A-Za-z '\-/]*?)\s+(\d.*)$", line)
            if m2:
                label = m2.group(1).strip()
                value = m2.group(2).strip()
                require_known = True

        if not label or value is None:
            # Free-form fallback: if the line contains a weight AND any known
            # equipment name as a phrase, pair them. Handles casual messages
            # like "i did 46kg on the leg curl" or "hit bench press 100kg today".
            fb = _freeform_match(line, freeform_re, custom_aliases)
            if fb is not None:
                canon_fb, weight_fb, bw_fb, reps_fb = fb
                if canon_fb not in seen:
                    seen.add(canon_fb)
                    lifts.append(Lift(
                        equipment=canon_fb, weight_kg=weight_fb,
                        bodyweight_add=bw_fb, raw=line, confident=True,
                        reps=reps_fb,
                    ))
            continue
        if not _looks_like_equipment(label):
            continue

        weight, bw_flag, confident = _extract_weight(value)
        if weight is None:
            continue
        reps = _extract_reps(value)

        canon = _resolve_with_custom(label, custom_aliases)
        if require_known and canon not in _KNOWN_CANONICALS \
                and canon not in custom_aliases.values():
            # Try the free-form fallback on this line before giving up.
            fb = _freeform_match(line, freeform_re, custom_aliases)
            if fb is not None:
                canon_fb, weight_fb, bw_fb, reps_fb = fb
                if canon_fb not in seen:
                    seen.add(canon_fb)
                    lifts.append(Lift(
                        equipment=canon_fb, weight_kg=weight_fb,
                        bodyweight_add=bw_fb, raw=line, confident=True,
                        reps=reps_fb,
                    ))
            continue
        if not canon or canon in seen:
            # Skip duplicate equipment within the same message (keep first).
            continue
        seen.add(canon)
        lifts.append(Lift(equipment=canon, weight_kg=weight,
                          bodyweight_add=bw_flag, raw=line,
                          confident=confident, reps=reps))

    return lifts


def estimated_one_rep_max(weight_kg: float, reps: int) -> float | None:
    """Epley 1RM estimate: 1RM = w * (1 + reps/30).

    Returns None for inputs that would produce a meaningless number
    (no weight, no reps, more than ~12 reps where Epley breaks down).
    """
    if weight_kg <= 0 or reps <= 0 or reps > 12:
        return None
    return round(weight_kg * (1 + reps / 30.0), 1)

