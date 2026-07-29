"""Message-wide scale tokens for per-100g label maths.

Australian food labels print energy and protein **per 100 g**, so logging what
you actually ate means scaling every number on the panel. The original syntax
put a multiplier in front of each one (``1.1x895kj 1.1x14.7p``), which asks for
two things at once: work out ``grams ÷ 100`` in your head, then type the answer
once per macro. Both are easy to get wrong, and a message that got either wrong
logged *nothing* — silently.

A *scale token* replaces both chores. Write the label numbers exactly as
printed and state the amount **once**, either as the weight you ate::

    895kj 14.7p 110g     # 110 g of a food labelled 895 kJ / 14.7 g per 100 g

or as an explicit multiplier::

    895kj 14.7p x1.1

The token may sit anywhere in the message and may be repeated, as long as every
copy agrees — so the way it comes out naturally, ``895kj x 1.1 14.7p x 1.1``,
works too. Tokens that *disagree* are refused rather than guessed at: picking
one would store a wrong number under a ✅, which is worse than not logging.

What separates a scale token from the older per-number prefix is which side the
symbol sits on. ``x1.1`` (symbol first) scales the whole message; ``1.1x895kj``
(symbol trailing its own number) scales just that number. Both still work, but
mixing them in one message is refused for the same reason.

Pure and Discord-free for direct unit testing.
"""
from __future__ import annotations

import itertools
import re
from dataclasses import dataclass

# A serving this heavy is a typo, not a meal, and a multiplier this big is a
# fat-fingered decimal point. Out-of-range tokens are refused rather than
# clamped, so the near-miss hint gets a chance to say why nothing was logged.
_MAX_GRAMS = 5_000.0
_MAX_FACTOR = 100.0

# Two factors are the same factor when they agree to within float wobble —
# `x1.1` and `110g` describe an identical serving and mustn't fight over it.
_EPSILON = 1e-9


@dataclass(frozen=True)
class Scale:
    """A resolved message-wide scale: the factor, plus how it was written."""

    factor: float
    label: str  # "110 g" / "×1.1" — echoed back so the maths stays auditable


# `x1.1`, `x 1.1`, `*1.1`, `×1.1`. The lookbehind is the whole trick: in
# `1.1x895kj` the symbol follows a digit, so it belongs to that 1.1 and is left
# for the per-number multiplier to deal with.
_MULT_TOKEN = re.compile(
    r"(?<![\w.])[x*×]\s*(?P<n>\d+(?:\.\d+)?|\.\d+)(?![\w.])",
    re.IGNORECASE,
)

# `110g`, `110 g`, `110grams`, and the spelled-out `@110g` / `of 110g` /
# `for 110g`. The trailing lookahead keeps this off a protein amount: in
# `40g protein` the grams ARE the protein, not the weight of the serving.
_GRAMS_TOKEN = re.compile(
    r"(?<![\w.])(?:@\s*|(?:of|for)\s+)?"
    r"(?P<n>\d+(?:\.\d+)?|\.\d+)\s*(?:grams?|g)"
    r"(?![\w.])(?!\s*(?:protein|prot|p)\b)",
    re.IGNORECASE,
)

# A per-number multiplier surviving next to a message-wide scale would scale
# that number twice (`1.1x895kj x1.1` = 1.21×), so the pair is refused.
_PREFIX_MULT = re.compile(r"\d\s*[x*×]\s*\d")


def split(text: str) -> tuple[str, Scale | None] | None:
    """Strip the message-wide scale token(s) out of *text*.

    Returns ``(remaining_text, scale)``, where ``scale`` is None when the
    message carries no scale token at all — in that case *text* comes back
    untouched, so callers pay nothing for the common case.

    Returns **None** when the message scales itself in two contradictory ways:
    tokens that disagree (``x1.1 … x1.2``), a token beside a per-number
    multiplier, or a weight/factor outside anything a human would eat. Callers
    must refuse to log rather than pick one of the readings.

    This reads tokens, not meaning — it will happily pull ``40g`` out of
    ``protein 40g``, where the grams are the protein. Nothing downstream is
    fooled, because :func:`resolve` only consults it once the literal reading
    has already failed, and ``protein 40g`` reads fine literally.
    """
    if not text:
        return text, None
    found: list[tuple[int, int, float, str]] = []  # start, end, factor, label
    for m in _MULT_TOKEN.finditer(text):
        factor = float(m.group("n"))
        found.append((m.start(), m.end(), factor, f"×{factor:g}"))
    for m in _GRAMS_TOKEN.finditer(text):
        grams = float(m.group("n"))
        if not 0 < grams <= _MAX_GRAMS:
            return None
        found.append((m.start(), m.end(), grams / 100.0, f"{grams:g} g"))
    if not found:
        return text, None

    factors = [f for _s, _e, f, _l in found]
    if any(not 0 < f <= _MAX_FACTOR for f in factors):
        return None
    if max(factors) - min(factors) > _EPSILON:
        return None  # the message scales itself two different ways

    spans = sorted((s, e) for s, e, _f, _l in found)
    for (_s1, e1), (s2, _e2) in itertools.pairwise(spans):
        if e1 > s2:
            return None  # one span read as two tokens — don't guess
    # Cut back-to-front so the earlier offsets stay valid, leaving a space
    # behind so `895kj x1.1 14.7p` doesn't weld into `895kj14.7p`.
    rest = text
    for start, end in reversed(spans):
        rest = rest[:start] + " " + rest[end:]
    rest = " ".join(rest.split())
    if _PREFIX_MULT.search(rest):
        return None  # `1.1x895kj x1.1` — which one wins? Neither.

    # Prefer the weight spelling when a message uses both: it's the one that
    # says what was actually eaten, rather than the arithmetic.
    label = next(
        (lbl for _s, _e, _f, lbl in found if lbl.endswith(" g")), found[0][3],
    )
    return rest, Scale(factor=factors[0], label=label)


def strip_tokens(text: str) -> str:
    """Remove every scale token from *text*, agreeing or not.

    Unlike :func:`split` this never refuses — it's for the near-miss check,
    which needs to know whether what's left over is still *words* (ordinary
    chat) or just the wreckage of a mistyped log.
    """
    out = _MULT_TOKEN.sub(" ", text or "")
    out = _GRAMS_TOKEN.sub(" ", out)
    return " ".join(out.split())


def resolve(text: str, match):
    """Run *match* on *text* as typed; failing that, on a scale-stripped copy.

    Returns ``(match_result, scale_applied)`` — with ``scale_applied`` None
    when the literal text parsed on its own — or None when neither attempt
    worked. Callers scale whichever part of the result is a number, because
    only they know which part that is.

    Trying the literal text **first** is what keeps scale tokens additive: no
    message that parsed before parses differently now, so ``180g`` still means
    180 g of protein to ``/protein setup`` rather than a serving weight.
    """
    hit = match(text or "")
    if hit is not None:
        return hit, None
    split_result = split(text or "")
    if split_result is None:
        return None  # the message scales itself two ways — refuse, don't guess
    body, scale = split_result
    if scale is None:
        return None  # nothing was stripped, so the retry would be identical
    hit = match(body)
    return None if hit is None else (hit, scale)


def apply(value: float, scale: Scale | None) -> float:
    """Scale *value*, or return it untouched when there's no scale."""
    return value if scale is None else value * scale.factor


def describe(scale: Scale | None) -> str | None:
    """One-line note for a log card, so the maths the bot did is visible.

    Silently applying a multiplier is the one failure mode worse than not
    logging, so every scaled entry says what it was scaled by.
    """
    if scale is None:
        return None
    if scale.label.endswith(" g"):
        return f"{scale.label} of the per-100 g values"
    return f"scaled {scale.label}"
