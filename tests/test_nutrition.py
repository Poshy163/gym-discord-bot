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
