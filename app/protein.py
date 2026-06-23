"""Protein (grams) parsing/formatting helpers.

Pure and Discord-free so it can be unit-tested directly. Protein is tracked in
grams against a personal **daily ceiling** — the point is to flag overeating,
not to chase a target. Mirrors the shape of :mod:`app.calories` but far simpler
(no unit conversion — grams are grams).
"""
from __future__ import annotations

import re

# A protein amount for slash commands: a number, optionally followed by "g"
# and/or a protein word. Accepts "180", "180g", "180 g protein".
_AMOUNT_RE = re.compile(
    r"^\s*(?P<num>\d+(?:\.\d+)?)\s*g?\s*(?:protein|prot|p)?\s*$",
    re.IGNORECASE,
)


def parse_protein_amount(text: str) -> float | None:
    """Parse a grams amount from free-form text, or None.

    Used by ``/protein setup`` and ``/protein add``. A bare number is grams.
    """
    m = _AMOUNT_RE.match(text or "")
    if m is None:
        return None
    return float(m.group("num"))


# Chat auto-logging is deliberately strict: the message must be ONLY a number
# plus an explicit protein marker ("p", "prot", "protein", optionally after a
# "g"), and nothing else. Requiring the marker keeps a bare "40g" or stray
# number from ever being logged — descriptions go through "/protein add".
_CHAT_RE = re.compile(
    r"^\s*(?P<num>\d{1,3}(?:\.\d+)?)\s*g?\s*(?:protein|prot|p)\b[\s.!?]*$",
    re.IGNORECASE,
)
# Reversed form: "protein 40", "protein 40g".
_CHAT_RE_REVERSED = re.compile(
    r"^\s*protein\s*(?P<num>\d{1,3}(?:\.\d+)?)\s*g?[\s.!?]*$",
    re.IGNORECASE,
)


def parse_protein_chat_message(text: str) -> float | None:
    """If ``text`` is *only* a protein amount with a marker, return grams.

    Matches ``40p``, ``40 p``, ``40g protein``, ``40 protein`` and the
    reversed ``protein 40``. Returns None for anything else (incl. a bare
    ``40g`` or a number alone) so the caller falls through to other parsers.
    """
    for rx in (_CHAT_RE, _CHAT_RE_REVERSED):
        m = rx.match(text or "")
        if m is not None:
            return float(m.group("num"))
    return None


def format_grams(grams: float) -> str:
    """Render a protein amount the way the bot displays it (whole grams)."""
    return f"{round(grams)} g"
