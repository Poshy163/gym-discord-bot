"""Prompts + response parsing for the AI nutrition features.

One command, ``/estimate``, and its chat shorthand ``~``. Both take a
description, a photo, or both, and both come back with calories + protein.
Two prompts sit behind that:

* :data:`ESTIMATE_SYSTEM` — words in, estimate out ("large big mac meal").
* :data:`PHOTO_SYSTEM` — a photo in. The photo is either a nutrition
  information panel or the food itself, and the *model* decides which rather
  than the person holding the camera: a panel is transcribed per 100 g, a
  plate is estimated for the portion shown. Asking someone to pick the right
  command before they know what the photo will turn out to be is a question
  nobody has the answer to, so :func:`parse_photo` tags the reply instead.

The Gemini calls themselves live in the bot layer (they need asyncio + the
shared client); everything here is pure so the prompt contracts and the
defensive JSON parsing can be unit-tested directly. Gemini is asked for JSON
but occasionally wraps it in markdown fences or prose — the parsers tolerate
that rather than failing the whole feature on cosmetic noise.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

# System prompt for meal estimates. Australian context matters: portion sizes
# and chains differ, and the users read kJ labels day-to-day.
ESTIMATE_SYSTEM = (
    "You are a nutrition estimator for an Australian fitness Discord bot. "
    "Given a short free-text description of a meal or snack, estimate its "
    "energy and protein. Use Australian portion sizes and menu items where "
    "relevant. Reply with ONLY a JSON object, no markdown fences, in this "
    'shape: {"kcal": <number>, "protein_g": <number or null>, '
    '"name": "<short cleaned-up name>", "confidence": "high|medium|low"}. '
    "kcal is kilocalories (not kJ) for the WHOLE described amount. If the "
    "description is not food or is impossible to estimate, reply "
    '{"error": "<short reason>"}.'
)

# System prompt for food photos, of either kind. The `kind` field is what
# lets one command serve both: a packet gets transcribed (per-100g, which
# composes with the scale-token syntax), a plate gets estimated for what's
# actually in shot. Panels win when both are visible — a transcription beats
# a guess whenever it's available.
PHOTO_SYSTEM = (
    "You read food photos for an Australian fitness Discord bot. The photo is "
    "either a nutrition information panel or the food itself. Decide which, "
    "and reply with ONLY a JSON object, no markdown fences.\n"
    "If a nutrition information panel is legible anywhere in the photo, "
    "transcribe its per-100g column and prefer that over estimating: "
    '{"kind": "label", "kj_per_100g": <number or null>, '
    '"kcal_per_100g": <number or null>, "protein_per_100g": <number or null>, '
    '"serving_g": <number or null>, "name": "<product name if visible, else '
    'null>"}. '
    "Energy on Australian panels is kilojoules — put that in kj_per_100g and "
    "only fill kcal_per_100g when the label itself prints kcal/Cal.\n"
    "Otherwise estimate the food you can see, for the WHOLE portion shown: "
    '{"kind": "meal", "kcal": <number>, "protein_g": <number or null>, '
    '"name": "<short cleaned-up name>", "confidence": "high|medium|low"}. '
    "kcal is kilocalories, not kJ. Use Australian portion sizes and menu "
    "items. Judge the portion from the plate, packet, hand or cutlery in "
    "shot, and say low confidence when there's nothing to judge scale by.\n"
    "If the photo shows neither food nor a nutrition panel, reply "
    '{"error": "<short reason>"}.'
)


@dataclass
class MealEstimate:
    kcal: float
    protein_g: float | None
    name: str
    confidence: str  # "high" | "medium" | "low" | "" when absent


@dataclass
class LabelInfo:
    kj_per_100g: float | None
    kcal_per_100g: float | None
    protein_per_100g: float | None
    serving_g: float | None
    name: str | None

    @property
    def has_energy(self) -> bool:
        return self.kj_per_100g is not None or self.kcal_per_100g is not None


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _first_bracket(text: str) -> int | None:
    """Index of the first ``{`` or ``[`` in ``text``, or None if neither."""
    idxs = [i for i in (text.find("{"), text.find("[")) if i != -1]
    return min(idxs) if idxs else None


def repair_unterminated_json(text: str) -> str | None:
    """Best-effort close of a JSON object/array that ends without its closing
    bracket(s).

    Some models drop the final ``}`` even on a reply they report as complete —
    notably ``gemini-3.5-flash`` under ``responseMimeType=application/json``,
    which returns e.g. ``{"kcal": 120, ... "confidence": "high"`` with no
    closing brace. Walk from the first bracket tracking string state, then
    append whatever brackets are needed to balance it. Returns the repaired
    string, or None when there's nothing open to fix (so callers only pay for
    it when the plain parse already failed).
    """
    start = _first_bracket(text)
    if start is None:
        return None
    stack: list[str] = []
    in_str = escape = False
    for ch in text[start:]:
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]" and stack:
            stack.pop()
    if not stack and not in_str:
        return None  # already balanced — the parse failed for some other reason
    repaired = text[start:]
    if in_str:
        repaired += '"'              # close a dangling string
    repaired = repaired.rstrip()
    if repaired.endswith(","):       # drop a trailing comma before the closers
        repaired = repaired[:-1]
    closers = {"{": "}", "[": "]"}
    repaired += "".join(closers[c] for c in reversed(stack))
    return repaired


def _extract_json(text: str) -> dict | None:
    """Pull the first JSON object out of a model reply.

    Tries the raw text, then any fenced block, then the outermost {...} span,
    then a bracket-balanced repair of an unterminated object (some models drop
    the closing brace even on a reply they report as complete). Returns None
    when nothing parses to a dict.
    """
    candidates = [text.strip()]
    m = _FENCE_RE.search(text)
    if m:
        candidates.append(m.group(1))
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    repaired = repair_unterminated_json(text)
    if repaired is not None:
        candidates.append(repaired)
    for cand in candidates:
        try:
            data = json.loads(cand)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _num(v: object) -> float | None:
    if isinstance(v, bool):  # bool is an int subclass; True is not a number here
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.replace(",", "").strip())
        except ValueError:
            return None
    return None


def _meal_from(data: dict) -> MealEstimate | str:
    """Build a :class:`MealEstimate` from a parsed reply dict, or an error."""
    kcal = _num(data.get("kcal"))
    if kcal is None or kcal <= 0:
        return "the AI couldn't put a number on that"
    protein = _num(data.get("protein_g"))
    if protein is not None and protein < 0:
        protein = None
    name = str(data.get("name") or "").strip()
    confidence = str(data.get("confidence") or "").strip().lower()
    if confidence not in ("high", "medium", "low"):
        confidence = ""
    return MealEstimate(
        kcal=kcal, protein_g=protein, name=name, confidence=confidence,
    )


def _label_from(data: dict) -> LabelInfo | str:
    """Build a :class:`LabelInfo` from a parsed reply dict, or an error."""
    info = LabelInfo(
        kj_per_100g=_num(data.get("kj_per_100g")),
        kcal_per_100g=_num(data.get("kcal_per_100g")),
        protein_per_100g=_num(data.get("protein_per_100g")),
        serving_g=_num(data.get("serving_g")),
        name=(str(data.get("name")).strip() or None)
        if data.get("name") else None,
    )
    # Negative numbers are transcription garbage, not label values.
    for field in ("kj_per_100g", "kcal_per_100g", "protein_per_100g", "serving_g"):
        v = getattr(info, field)
        if v is not None and v < 0:
            setattr(info, field, None)
    if not info.has_energy and info.protein_per_100g is None:
        return "couldn't read any energy or protein values off that label"
    return info


def parse_estimate(text: str) -> MealEstimate | str:
    """Parse a meal-estimate reply. Returns a :class:`MealEstimate`, or a
    short human-readable error string when the model declined / the reply is
    unusable (callers show it directly)."""
    data = _extract_json(text)
    if data is None:
        return "the AI reply wasn't in the expected format"
    if data.get("error"):
        return str(data["error"])[:200]
    return _meal_from(data)


def parse_photo(text: str) -> MealEstimate | LabelInfo | str:
    """Parse a photo reply into whichever shape the model chose.

    A :class:`LabelInfo` means it found a nutrition panel and transcribed it
    (per 100 g, so it still needs a serving weight before anything is logged);
    a :class:`MealEstimate` means it looked at food and guessed the portion in
    shot. A string is a short human-readable error.

    ``kind`` is what the prompt asks for, but a model that drops the tag still
    gives itself away by which fields it filled — and getting that wrong would
    log a per-100g figure as if it were a whole meal, so the fallback checks
    the fields rather than defaulting to either shape.
    """
    data = _extract_json(text)
    if data is None:
        return "the AI reply wasn't in the expected format"
    if data.get("error"):
        return str(data["error"])[:200]
    kind = str(data.get("kind") or "").strip().lower()
    if kind == "label":
        return _label_from(data)
    if kind == "meal":
        return _meal_from(data)
    per_100g = any(
        data.get(k) is not None
        for k in ("kj_per_100g", "kcal_per_100g", "protein_per_100g")
    )
    return _label_from(data) if per_100g else _meal_from(data)
