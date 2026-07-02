"""Combined nutrition parsing.

A single chat message can log BOTH a calorie amount and a protein amount, e.g.
``500c and 40p`` or ``40g protein 2700kj``. This module finds one calorie token
and one protein token and confirms the rest of the message is only connectors
("and", commas, ``+``…) — staying as strict as the single-token parsers so
casual chatter is never logged.

Pure and Discord-free for direct unit testing.
"""
from __future__ import annotations

import re

from . import calories

# Both tokens accept the same optional multiplier prefix as the single-token
# parsers, for per-100g label maths (`0.7x1640kj and 0.7x43p`). The protein
# pattern needs a distinct group name per alternative (m1/m2) because a group
# name can't repeat within one regex.
# Un-anchored (findable) calorie + protein tokens.
_CAL = re.compile(
    r"(?:(?P<mult>\d+(?:\.\d+)?|\.\d+)\s*[x*×]\s*)?"
    r"(?P<num>\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)\s*"
    r"(?P<unit>kcal|cals?|calories?|kj|kilojoules?|c)\b",
    re.IGNORECASE,
)
_PRO = re.compile(
    r"(?:(?:(?P<m1>\d+(?:\.\d+)?|\.\d+)\s*[x*×]\s*)?"
    r"(?P<g1>\d+(?:\.\d+)?)\s*g?\s*(?:protein|prot|p)\b"
    r"|protein\s*(?:(?P<m2>\d+(?:\.\d+)?|\.\d+)\s*[x*×]\s*)?"
    r"(?P<g2>\d+(?:\.\d+)?)\s*g?\b)",
    re.IGNORECASE,
)
# Words/symbols allowed to sit between (and around) the two tokens.
_CONNECTOR_WORDS = re.compile(r"(?i)\b(?:and|with|plus|n)\b")
_CONNECTOR_CHARS = re.compile(r"[\s,+&./!?-]")

_KJ_UNITS = {"kj", "kilojoule"}


def parse_combined(text: str) -> tuple[float, float] | None:
    """Return ``(kcal, protein_g)`` when *text* is exactly one calorie token and
    one protein token joined by connectors, else None.

    Requires **both** tokens — a message with only one falls through to the
    single-amount parsers. Calorie kJ values are converted to kcal.
    """
    if not text:
        return None
    s = text.strip()
    cal_m = _CAL.search(s)
    pro_m = _PRO.search(s)
    if cal_m is None or pro_m is None:
        return None
    spans = sorted([cal_m.span(), pro_m.span()])
    if spans[0][1] > spans[1][0]:
        return None  # the two tokens overlap — not a clean pair

    remainder = (
        s[: spans[0][0]] + s[spans[0][1] : spans[1][0]] + s[spans[1][1] :]
    )
    remainder = _CONNECTOR_WORDS.sub(" ", remainder)
    remainder = _CONNECTOR_CHARS.sub("", remainder)
    if remainder:
        return None  # leftover words → it's a sentence, not a log

    num = float(cal_m.group("num").replace(",", ""))
    if cal_m.group("mult"):
        num *= float(cal_m.group("mult"))
    unit = cal_m.group("unit").lower().rstrip("s")
    kcal = calories.kj_to_kcal(num) if unit in _KJ_UNITS else num
    grams = float(pro_m.group("g1") or pro_m.group("g2"))
    pro_mult = pro_m.group("m1") or pro_m.group("m2")
    if pro_mult:
        grams *= float(pro_mult)
    return kcal, grams
