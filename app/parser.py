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

from __future__ import annotations

import re
from dataclasses import dataclass

from .aliases import canonicalize

# Set of all canonical equipment names known to the alias table. Used to
# gate "label number" lines where no weight unit is given.
from .aliases import _ALIAS_GROUPS as _AG  # noqa: PLC2701  (internal use)
_KNOWN_CANONICALS: set[str] = set(_AG.keys())

# All alias phrases (canonical + aliases) as lowercase strings, sorted longest
# first so "leg press" matches before "press" when scanning free-form text.
_ALL_ALIAS_PHRASES: list[str] = sorted(
    {p.lower() for canon, aliases in _AG.items() for p in (canon, *aliases)},
    key=lambda s: (-len(s), s),
)
_FREEFORM_ALIAS_RE = re.compile(
    r"\b(" + "|".join(re.escape(p) for p in _ALL_ALIAS_PHRASES) + r")\b",
    re.IGNORECASE,
)

# Assumed plate weight in kg (standard Olympic plate per side).
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
_COLON_RE = re.compile(rf"^\s*([A-Za-z][A-Za-z '\-/]{{1,60}}?)\s*[:\-]\s*(.+?)\s*$")

# Matches a value portion and pulls out a weight.
#   "45kg", "45 kg", "BW+20kg", "6 plates", "3.5 plates",
#   "50 - 77 kg", "45kg L and R", "80kg", "BW"
_RANGE_RE = re.compile(rf"({_NUM})\s*-\s*({_NUM})\s*kg?", re.IGNORECASE)
_BW_PLUS_RE = re.compile(rf"bw\s*\+\s*({_NUM})\s*kg?", re.IGNORECASE)
_PLATES_RE = re.compile(rf"({_NUM})\s*plates?", re.IGNORECASE)
_KG_RE = re.compile(rf"({_NUM})\s*kg", re.IGNORECASE)
_BARE_NUM_RE = re.compile(rf"(?<![\w.])({_NUM})(?!\s*(?:rm|rep|reps|set|sets))", re.IGNORECASE)
_BW_RE = re.compile(r"\bbw\b", re.IGNORECASE)

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


def _freeform_match(line: str) -> tuple[str, float, bool] | None:
    """Scan a free-form sentence for (equipment, weight_kg, bodyweight_add).

    Returns None unless the line contains both a known equipment phrase and
    a weight carrying an explicit unit. Avoids false positives from casual
    chat.
    """
    m = _FREEFORM_ALIAS_RE.search(line)
    if not m:
        return None
    canon = canonicalize(m.group(1))
    if canon not in _KNOWN_CANONICALS:
        return None

    # Only accept weights with an explicit unit in free-form — bare numbers
    # are too ambiguous in a sentence.
    bw_plus = _BW_PLUS_RE.search(line)
    if bw_plus:
        return canon, float(bw_plus.group(1)), True
    rng = _RANGE_RE.search(line)
    if rng:
        return canon, max(float(rng.group(1)), float(rng.group(2))), False
    plates = _PLATES_RE.search(line)
    if plates:
        return canon, float(plates.group(1)) * PLATE_KG, False
    kg = _KG_RE.search(line)
    if kg:
        return canon, float(kg.group(1)), False
    return None


def parse_message(text: str) -> list[Lift]:
    """Parse a whole message and return any lifts detected."""
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
            fb = _freeform_match(line)
            if fb is not None:
                canon_fb, weight_fb, bw_fb = fb
                if canon_fb not in seen:
                    seen.add(canon_fb)
                    lifts.append(Lift(
                        equipment=canon_fb, weight_kg=weight_fb,
                        bodyweight_add=bw_fb, raw=line, confident=True,
                    ))
            continue
        if not _looks_like_equipment(label):
            continue

        weight, bw_flag, confident = _extract_weight(value)
        if weight is None:
            continue

        canon = canonicalize(label)
        if require_known and canon not in _KNOWN_CANONICALS:
            # Try the free-form fallback on this line before giving up.
            fb = _freeform_match(line)
            if fb is not None:
                canon_fb, weight_fb, bw_fb = fb
                if canon_fb not in seen:
                    seen.add(canon_fb)
                    lifts.append(Lift(
                        equipment=canon_fb, weight_kg=weight_fb,
                        bodyweight_add=bw_fb, raw=line, confident=True,
                    ))
            continue
        if not canon or canon in seen:
            # Skip duplicate equipment within the same message (keep first).
            continue
        seen.add(canon)
        lifts.append(Lift(equipment=canon, weight_kg=weight,
                          bodyweight_add=bw_flag, raw=line,
                          confident=confident))

    return lifts
