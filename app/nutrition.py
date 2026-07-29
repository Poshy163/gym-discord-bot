"""Combined nutrition parsing.

A single chat message can log BOTH a calorie amount and a protein amount, e.g.
``500c and 40p`` or ``40g protein 2700kj``. This module finds one calorie token
and one protein token and confirms the rest of the message is only connectors
("and", commas, ``+``…) — staying as strict as the single-token parsers so
casual chatter is never logged.

It's also where a nutrition panel gets logged whole: a label prints both macros
per 100 g, so ``895kj 14.7p 110g`` scales the pair together off one stated
serving (see :mod:`app.scaling`).

Being strict has a cost, though — a message that misses logs *nothing*, and
nothing looks exactly like a successful log to anyone who isn't watching for
the ✅. :func:`near_miss` pays that back by explaining the misses that were
obviously trying to be food logs.

Pure and Discord-free for direct unit testing.
"""
from __future__ import annotations

import re

from . import calories, protein, scaling

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


def match_combined(text: str) -> tuple[float, float] | None:
    """Match a combined amount **exactly as typed**, applying no message-wide
    scale. The scaled half is :func:`parse_combined`."""
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


def parse_combined(text: str) -> tuple[float, float] | None:
    """Return ``(kcal, protein_g)`` when *text* is exactly one calorie token and
    one protein token joined by connectors, else None.

    Requires **both** tokens — a message with only one falls through to the
    single-amount parsers. Calorie kJ values are converted to kcal.

    This is where a scale token earns its keep: a nutrition panel gives both
    macros per 100 g, so ``895kj 14.7p 110g`` logs a whole serving off the
    packet with the arithmetic stated once instead of twice. Both macros take
    the same scale — a message that tries to scale them differently is refused
    rather than half-applied (see :mod:`app.scaling`).
    """
    resolved = scaling.resolve(text, match_combined)
    if resolved is None:
        return None
    (kcal, grams), scale = resolved
    return scaling.apply(kcal, scale), scaling.apply(grams, scale)


def chat_scale(text: str) -> scaling.Scale | None:
    """The scale the chat auto-loggers actually applied to *text*, or None.

    A message that parses **as typed** was never scaled, whatever tokens it
    happens to contain — ``protein 40g`` is 40 g of protein, not 40 g of a
    per-100g food — so the literal readings are checked first, exactly as the
    parsers check them.
    """
    if not text:
        return None
    if (
        calories.match_chat_energy(text) is not None
        or protein.match_chat_protein(text) is not None
        or match_combined(text) is not None
    ):
        return None
    split_result = scaling.split(text)
    return None if split_result is None else split_result[1]


def chat_scale_note(text: str) -> str | None:
    """Card note describing how a chat auto-log was scaled, or None."""
    return scaling.describe(chat_scale(text))


# Tokens strong enough to say "this was *meant* to be a food log": an energy
# unit, or a number carrying a protein marker. A bare number or a lone "c"
# isn't enough — those are lift posts and ordinary chat.
_NUTRIENT_HINT = re.compile(
    r"\d\s*(?:kcal|cals?|calories?|kj|kilojoules?)\b"
    r"|\d\s*g?\s*(?:protein|prot)\b"
    r"|\d\s*p\b",
    re.IGNORECASE,
)
_NEAR_MISS_MAX_LEN = 80


def near_miss(text: str) -> str | None:
    """Explain why a food-log-shaped message logged nothing, or None.

    Auto-logging is all-or-nothing and used to fail *silently*: the only
    difference between "logged" and "ignored" was a ✅ that wasn't there. A
    message whose scale didn't come through (``895kj x 1.1 14.7p x 1.2``)
    therefore looked exactly like a successful log to anyone not watching the
    reactions, and the entry just went missing.

    Returns None for anything that logged fine and for anything that never
    looked like a log — once the recognised tokens are removed, a message with
    *words* still in it is chat, and callers must not nag chat.
    """
    if not text or "\n" in text:
        return None
    s = text.strip()
    if not s or len(s) > _NEAR_MISS_MAX_LEN:
        return None
    if not _NUTRIENT_HINT.search(s):
        return None
    if (
        calories.parse_chat_message(s) is not None
        or protein.parse_protein_chat_message(s) is not None
        or parse_combined(s) is not None
    ):
        return None  # it logged — there's nothing to explain

    residue = _CONNECTOR_WORDS.sub(" ", scaling.strip_tokens(s))
    residue = _PRO.sub(" ", _CAL.sub(" ", residue))
    if any(ch.isalpha() for ch in residue):
        return None  # real words left over — chat that mentions food, not a log
    if scaling.split(s) is None:
        return (
            "that message scales itself two different ways, so I didn't want "
            "to guess which one you meant. Say the amount once — "
            "`895kj 14.7p 110g`, or `895kj 14.7p x1.1`."
        )
    return (
        "that looked like a food log but I couldn't read all of it. The whole "
        "message has to be the amounts — `895kj 14.7p` for the label values, "
        "plus `110g` (or `x1.1`) once if they're per 100 g."
    )
