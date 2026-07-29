"""Tests for combined calorie+protein chat parsing (app/nutrition.py)."""
from __future__ import annotations

import pytest

from app import calories, nutrition


def test_parse_combined_basic():
    assert nutrition.parse_combined("500c and 40p") == (500.0, 40.0)
    # Order independent.
    assert nutrition.parse_combined("40p and 500c") == (500.0, 40.0)
    # Various connectors / spacing.
    assert nutrition.parse_combined("500c 40p") == (500.0, 40.0)
    assert nutrition.parse_combined("500 cal + 40g protein") == (500.0, 40.0)
    assert nutrition.parse_combined("40 protein, 500 calories") == (500.0, 40.0)


def test_parse_combined_kj_converted():
    kcal, grams = nutrition.parse_combined("2700kj and 40p")
    assert grams == 40.0
    assert kcal == pytest.approx(calories.kj_to_kcal(2700))


def test_parse_combined_requires_both_tokens():
    # Only one of the two → None (single-amount parsers handle those).
    assert nutrition.parse_combined("500c") is None
    assert nutrition.parse_combined("40p") is None
    assert nutrition.parse_combined("") is None
    assert nutrition.parse_combined("just chatting") is None


def test_parse_combined_rejects_sentences():
    # Extra words beyond the two tokens + connectors → not a clean log.
    assert nutrition.parse_combined("had 500c and 40p for lunch") is None
    assert nutrition.parse_combined("500c and 40p is crazy work") is None


def test_parse_combined_multiplier():
    # Per-100g label maths on either (or both) tokens: 70g of a food listing
    # 1640 kJ and 43 g protein per 100 g.
    kcal, grams = nutrition.parse_combined("0.7x1640kj and 0.7x43p")
    assert kcal == pytest.approx(calories.kj_to_kcal(0.7 * 1640))
    assert grams == pytest.approx(30.1)
    # Order independent, and plain + multiplied tokens can mix.
    kcal, grams = nutrition.parse_combined("0.7x43p and 500c")
    assert (kcal, grams) == (500.0, pytest.approx(30.1))
    kcal, grams = nutrition.parse_combined("0.5x800c 2x20p")
    assert (kcal, grams) == (400.0, 40.0)
    # Still rejects sentences.
    assert nutrition.parse_combined("had 0.7x1640kj and 0.7x43p today") is None


# ---- scale tokens (per-100g labels, serving stated once) -------------------

@pytest.mark.parametrize("text", [
    # A nutrition panel gives both macros per 100 g; these all say "and I ate
    # 110 g of it" exactly once, instead of pre-multiplying each number.
    "895kj 14.7p 110g",
    "895kj 14.7p 110 g",
    "895kj 14.7p @110g",
    "895kj 14.7p x1.1",
    "x1.1 895kj 14.7p",
    "895kj and 14.7p 110g",
    # Written out per macro, which is how it comes out when you're reading the
    # packet — the copies agree, so it resolves to the one scale.
    "895kj x 1.1 14.7p x 1.1",
])
def test_parse_combined_scale_token(text):
    result = nutrition.parse_combined(text)
    assert result is not None
    kcal, grams = result
    assert kcal == pytest.approx(calories.kj_to_kcal(895 * 1.1))
    assert grams == pytest.approx(14.7 * 1.1)


@pytest.mark.parametrize("text", [
    "895kj x1.1 14.7p x1.2",   # the copies disagree — don't pick one
    "1.1x895kj 14.7p x1.1",    # prefix multiplier beside a scale token
    "895kj 14.7p 110g 120g",
    "895kj 14.7p 0g",
    "had 895kj 14.7p 110g for lunch",  # still a sentence, still not a log
])
def test_parse_combined_refuses_ambiguous_scale(text):
    assert nutrition.parse_combined(text) is None


def test_chat_scale_note_shows_its_working():
    # Silently applying a multiplier is worse than not logging, so a scaled
    # entry carries a note saying what it was scaled by.
    assert (
        nutrition.chat_scale_note("895kj 14.7p 110g")
        == "110 g of the per-100 g values"
    )
    assert nutrition.chat_scale_note("895kj 14.7p x1.1") == "scaled ×1.1"
    assert nutrition.chat_scale_note("2700kj 110g") == "110 g of the per-100 g values"
    # Nothing was scaled → nothing to say.
    assert nutrition.chat_scale_note("500c and 40p") is None
    assert nutrition.chat_scale_note("650kcal") is None
    # A message that reads fine literally was never scaled, whatever tokens it
    # happens to contain.
    assert nutrition.chat_scale_note("protein 40g") is None
    assert nutrition.chat_scale_note("40g protein") is None


# ---- near-miss hints -------------------------------------------------------

def test_near_miss_explains_a_contradictory_scale():
    hint = nutrition.near_miss("895kj x 1.1 14.7p x 1.2")
    assert hint is not None
    assert "two different ways" in hint


def test_near_miss_explains_an_unreadable_log():
    # The `g` didn't make it, so the 110 is just a stray number.
    hint = nutrition.near_miss("895kj 14.7p 110")
    assert hint is not None
    assert "110g" in hint


@pytest.mark.parametrize("text", [
    # Logged fine — there's nothing to explain.
    "895kj 14.7p 110g",
    "500c and 40p",
    "650kcal",
    "40p",
    # Never looked like a log: once the amounts come out, real words are left.
    "I ate 2000 calories today",
    "1500cal is crazy work",
    "650kcal burrito",
    "bench 100kg 3p",       # plates, not protein
    "3x5 100kg",
    "just chatting",
    "",
    # Multi-line dumps belong to the lift parser, not to a nutrition nag.
    "650kcal\nbench press 80kg",
])
def test_near_miss_stays_quiet(text):
    assert nutrition.near_miss(text) is None


def test_near_miss_ignores_long_messages():
    # A paragraph that happens to contain "40p" is a paragraph, not a log.
    assert nutrition.near_miss("40p " + "x" * 100) is None
