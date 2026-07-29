"""Tests for message-wide scale tokens (app/scaling.py)."""
from __future__ import annotations

import pytest

from app import scaling


# ---- tokens that resolve ---------------------------------------------------

@pytest.mark.parametrize("text,rest,factor,label", [
    # The weight spelling: state what you ate, the bot does grams ÷ 100.
    ("895kj 14.7p 110g", "895kj 14.7p", 1.1, "110 g"),
    ("895kj 14.7p 110 g", "895kj 14.7p", 1.1, "110 g"),
    ("895kj 14.7p 110grams", "895kj 14.7p", 1.1, "110 g"),
    ("895kj 14.7p @110g", "895kj 14.7p", 1.1, "110 g"),
    ("895kj 14.7p of 110g", "895kj 14.7p", 1.1, "110 g"),
    ("895kj 14.7p for 110g", "895kj 14.7p", 1.1, "110 g"),
    ("70g 1640kj", "1640kj", 0.7, "70 g"),
    # The multiplier spelling, symbol first so it can't be read as a prefix.
    ("895kj 14.7p x1.1", "895kj 14.7p", 1.1, "×1.1"),
    ("895kj 14.7p x 1.1", "895kj 14.7p", 1.1, "×1.1"),
    ("895kj 14.7p *1.1", "895kj 14.7p", 1.1, "×1.1"),
    ("895kj 14.7p ×1.1", "895kj 14.7p", 1.1, "×1.1"),
    ("x1.1 895kj 14.7p", "895kj 14.7p", 1.1, "×1.1"),
    # Repeated tokens are fine as long as they agree — which is how it comes
    # out when you write one per macro.
    ("895kj x 1.1 14.7p x 1.1", "895kj 14.7p", 1.1, "×1.1"),
    # Both spellings at once, agreeing: the weight wins the label because it
    # says what was eaten rather than the arithmetic.
    ("895kj x1.1 14.7p 110g", "895kj 14.7p", 1.1, "110 g"),
])
def test_split_extracts_scale(text, rest, factor, label):
    result = scaling.split(text)
    assert result is not None
    got_rest, scale = result
    assert got_rest == rest
    assert scale is not None
    assert scale.factor == pytest.approx(factor)
    assert scale.label == label


# ---- text that carries no scale token at all -------------------------------

@pytest.mark.parametrize("text", [
    # The per-number prefix keeps its meaning: the symbol trails its own
    # number, so it belongs to that number and isn't a message-wide scale.
    "0.7x1640kj",
    "0.7x1640kj and 0.7x43p",
    "2x20p",
    # Lift posts must stay invisible to this — a set count is not a scale.
    "3x5 100kg",
    "bench 100kg",
    "650kcal",
    "40g protein",   # the grams ARE the protein here, not a serving weight
    "40p",
    "protein 40",
    "",
])
def test_split_finds_nothing(text):
    result = scaling.split(text)
    assert result is not None
    got_rest, scale = result
    assert scale is None
    assert got_rest == text  # untouched when there's nothing to strip


def test_split_is_context_blind_and_that_is_fine():
    # split() reads tokens, not meaning: in `protein 40g` it can't see that the
    # grams belong to the protein, because the disambiguating word is behind
    # the token rather than in front of it.
    rest, scale = scaling.split("protein 40g")
    assert (rest, scale.factor) == ("protein", pytest.approx(0.4))
    # It doesn't matter, because resolve() only ever consults split() *after*
    # the literal reading has failed — and `protein 40g` reads fine literally.
    # See test_protein.py for the parser-level guarantee.


# ---- contradictions are refused, never guessed -----------------------------

@pytest.mark.parametrize("text", [
    "895kj x1.1 14.7p x1.2",      # two different multipliers
    "895kj 110g 14.7p 120g",      # two different weights
    "895kj x1.1 14.7p 120g",      # a multiplier and a weight that disagree
    "1.1x895kj x1.1",             # per-number prefix beside a scale token
    "895kj 14.7p 0g",             # a zero serving isn't a meal
    "895kj 14.7p 9000g",          # nine kilos is a typo
    "895kj 14.7p x0",
])
def test_split_refuses_contradictions(text):
    assert scaling.split(text) is None


def test_strip_tokens_never_refuses():
    # strip_tokens exists for the near-miss check, which has to see what's
    # left over even when split() gave up on the message.
    assert scaling.strip_tokens("895kj x1.1 14.7p x1.2") == "895kj 14.7p"
    assert scaling.strip_tokens("895kj 14.7p 110g") == "895kj 14.7p"
    assert scaling.strip_tokens("I ate 2000 calories") == "I ate 2000 calories"
    assert scaling.strip_tokens("") == ""


def test_apply_and_describe():
    _rest, scale = scaling.split("895kj 110g")
    assert scaling.apply(100.0, scale) == pytest.approx(110.0)
    assert scaling.describe(scale) == "110 g of the per-100 g values"

    _rest, scale = scaling.split("895kj x1.1")
    assert scaling.describe(scale) == "scaled ×1.1"

    assert scaling.apply(100.0, None) == 100.0
    assert scaling.describe(None) is None


def test_resolve_prefers_the_literal_reading():
    # A message that parses as typed is never scaled, whatever tokens it holds.
    # This is what keeps `180g` meaning 180 g to /protein setup.
    resolved = scaling.resolve("180g", lambda t: 180.0 if t == "180g" else None)
    assert resolved == (180.0, None)

    # Only when the literal reading fails does the scale token get used.
    resolved = scaling.resolve("43p 70g", lambda t: 43.0 if t == "43p" else None)
    assert resolved is not None
    value, scale = resolved
    assert value == 43.0
    assert scale.factor == pytest.approx(0.7)

    # Neither reading works → None, and so does a message that refuses to
    # resolve its own scale.
    assert scaling.resolve("nonsense", lambda _t: None) is None
    assert scaling.resolve("43p x1.1 x1.2", lambda _t: 43.0 if _t == "43p" else None) is None
