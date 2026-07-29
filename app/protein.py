"""Protein (grams) parsing/formatting helpers.

Pure and Discord-free so it can be unit-tested directly. Protein is tracked in
grams against a personal **daily ceiling** — the point is to flag overeating,
not to chase a target. Mirrors the shape of :mod:`app.calories` but far simpler
(no unit conversion — grams are grams).
"""
from __future__ import annotations

import re

from . import scaling

# Optional multiplier prefix for label maths: labels list protein per 100 g,
# so eating 70 g of a "43 g per 100 g" food is `0.7x43p` (= 30.1 g). Mirrors
# the calorie parsers' multiplier. Accepts x / * / ×. The friendlier spelling
# — `43p 70g`, stating the serving once — comes from `app.scaling`.
_MULT = r"(?:(?P<mult>\d+(?:\.\d+)?|\.\d+)\s*[x*×]\s*)?"

# A protein amount for slash commands: a number, optionally followed by "g"
# and/or a protein word. Accepts "180", "180g", "180 g protein", "0.7x43".
_AMOUNT_RE = re.compile(
    rf"^\s*{_MULT}(?P<num>\d+(?:\.\d+)?)\s*g?\s*(?:protein|prot|p)?\s*$",
    re.IGNORECASE,
)


def _apply_mult(m: re.Match[str]) -> float:
    grams = float(m.group("num"))
    if m.group("mult"):
        grams *= float(m.group("mult"))
    return grams


def match_protein_amount(text: str) -> float | None:
    """Match a grams amount **exactly as typed**, applying no message-wide
    scale. The scaled half is :func:`parse_protein_amount`."""
    m = _AMOUNT_RE.match(text or "")
    if m is None:
        return None
    return _apply_mult(m)


def parse_protein_amount(text: str) -> float | None:
    """Parse a grams amount from free-form text, or None.

    Used by ``/protein setup`` and ``/protein add``. A bare number is grams;
    a multiplier prefix scales it (``0.7x43`` → 30.1 g for per-100g labels),
    as does a message-wide scale token (``43p 70g``). A plain ``180g`` still
    means 180 g of protein — the literal reading is always tried first.
    """
    resolved = scaling.resolve(text, match_protein_amount)
    if resolved is None:
        return None
    grams, scale = resolved
    return scaling.apply(grams, scale)


# Chat auto-logging is deliberately strict: the message must be ONLY a number
# plus an explicit protein marker ("p", "prot", "protein", optionally after a
# "g"), and nothing else. Requiring the marker keeps a bare "40g" or stray
# number from ever being logged — descriptions go through "/protein add".
_CHAT_RE = re.compile(
    rf"^\s*{_MULT}(?P<num>\d{{1,3}}(?:\.\d+)?)\s*g?\s*(?:protein|prot|p)\b[\s.!?]*$",
    re.IGNORECASE,
)
# Reversed form: "protein 40", "protein 40g".
_CHAT_RE_REVERSED = re.compile(
    rf"^\s*protein\s*{_MULT}(?P<num>\d{{1,3}}(?:\.\d+)?)\s*g?[\s.!?]*$",
    re.IGNORECASE,
)


def match_chat_protein(text: str) -> float | None:
    """Match a chat protein amount **exactly as typed**, applying no
    message-wide scale. The scaled half is :func:`parse_protein_chat_message`."""
    for rx in (_CHAT_RE, _CHAT_RE_REVERSED):
        m = rx.match(text or "")
        if m is not None:
            return _apply_mult(m)
    return None


def parse_protein_chat_message(text: str) -> float | None:
    """If ``text`` is *only* a protein amount with a marker, return grams.

    Matches ``40p``, ``40 p``, ``40g protein``, ``40 protein``, the reversed
    ``protein 40``, and per-100g label maths — either as a prefix multiplier
    (``0.7x43p``) or as a scale token stating the serving once (``43p 70g``,
    ``43p x0.7``). Returns None for anything else (incl. a bare ``40g`` or a
    number alone) so the caller falls through to other parsers.
    """
    resolved = scaling.resolve(text, match_chat_protein)
    if resolved is None:
        return None
    grams, scale = resolved
    return scaling.apply(grams, scale)


def format_grams(grams: float) -> str:
    """Render a protein amount the way the bot displays it (whole grams)."""
    return f"{round(grams)} g"
