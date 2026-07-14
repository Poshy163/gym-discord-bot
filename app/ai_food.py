"""Prompts + response parsing for the AI nutrition features.

Two features share this module:

* **Meal estimates** (``/calories estimate`` or a chat message starting with
  ``~``): describe a meal in words, Gemini guesses calories + protein.
* **Label reading** (``/calories label``): photograph a nutrition information
  panel, Gemini transcribes the per-100g values.

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

# System prompt for nutrition-panel photos. The panel prints per-100g and
# per-serving columns; we want per-100g (it composes with the bot's
# multiplier syntax) plus the stated serving size when readable.
LABEL_SYSTEM = (
    "You read Australian nutrition information panels from photos. Extract "
    "the per-100g column. Reply with ONLY a JSON object, no markdown fences: "
    '{"kj_per_100g": <number or null>, "kcal_per_100g": <number or null>, '
    '"protein_per_100g": <number or null>, "serving_g": <number or null>, '
    '"name": "<product name if visible, else null>"}. '
    "Energy on Australian panels is kilojoules — put that in kj_per_100g and "
    "only fill kcal_per_100g when the label itself prints kcal/Cal. If the "
    "image does not show a readable nutrition panel, reply "
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


def parse_estimate(text: str) -> MealEstimate | str:
    """Parse a meal-estimate reply. Returns a :class:`MealEstimate`, or a
    short human-readable error string when the model declined / the reply is
    unusable (callers show it directly)."""
    data = _extract_json(text)
    if data is None:
        return "the AI reply wasn't in the expected format"
    if data.get("error"):
        return str(data["error"])[:200]
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


def parse_label(text: str) -> LabelInfo | str:
    """Parse a label-photo reply. Returns :class:`LabelInfo` or a short
    human-readable error string."""
    data = _extract_json(text)
    if data is None:
        return "the AI reply wasn't in the expected format"
    if data.get("error"):
        return str(data["error"])[:200]
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
