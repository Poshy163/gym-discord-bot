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
