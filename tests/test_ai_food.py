"""Tests for AI nutrition response parsing (app/ai_food.py)."""
from __future__ import annotations

from app.ai_food import (
    LabelInfo,
    MealEstimate,
    parse_estimate,
    parse_label,
    repair_unterminated_json,
)


# ---- parse_estimate ---------------------------------------------------------

def test_parse_estimate_clean_json():
    est = parse_estimate(
        '{"kcal": 1050, "protein_g": 38, "name": "Large Big Mac meal", '
        '"confidence": "medium"}'
    )
    assert isinstance(est, MealEstimate)
    assert est.kcal == 1050.0
    assert est.protein_g == 38.0
    assert est.name == "Large Big Mac meal"
    assert est.confidence == "medium"


def test_parse_estimate_fenced_json():
    est = parse_estimate(
        'Sure! Here you go:\n```json\n{"kcal": 420, "protein_g": null, '
        '"name": "banana bread slice"}\n```'
    )
    assert isinstance(est, MealEstimate)
    assert est.kcal == 420.0
    assert est.protein_g is None
    assert est.confidence == ""  # absent → empty


def test_parse_estimate_error_object():
    out = parse_estimate('{"error": "that is not food"}')
    assert out == "that is not food"


def test_parse_estimate_garbage():
    assert isinstance(parse_estimate("I can't help with that."), str)
    assert isinstance(parse_estimate(""), str)
    assert isinstance(parse_estimate('{"kcal": "lots"}'), str)
    assert isinstance(parse_estimate('{"kcal": -5}'), str)
    assert isinstance(parse_estimate("[1, 2, 3]"), str)


def test_parse_estimate_truncated_json():
    # A reply cut off mid-string is repaired when it still yields a usable
    # object — the valid kcal survives even though the trailing name was cut.
    est = parse_estimate('{"kcal": 90, "name": "flat wh')
    assert isinstance(est, MealEstimate)
    assert est.kcal == 90.0
    # A bare JSON string isn't an object, so it stays an error...
    assert isinstance(parse_estimate('"a bare json string"'), str)
    # ...and a value cut off before it exists can't be repaired into a number.
    assert isinstance(parse_estimate('{"kcal":'), str)


def test_parse_estimate_missing_closing_brace():
    # gemini-3.5-flash under JSON mode drops the final "}" on a *complete*
    # (finishReason=STOP) reply — the parser must still recover it.
    est = parse_estimate(
        '{\n  "kcal": 120,\n  "protein_g": 7.0,\n'
        '  "name": "Flat white no sugar",\n  "confidence": "high"'
    )
    assert isinstance(est, MealEstimate)
    assert est.kcal == 120.0
    assert est.protein_g == 7.0
    assert est.name == "Flat white no sugar"
    assert est.confidence == "high"


def test_parse_label_missing_closing_brace():
    info = parse_label(
        '{"kj_per_100g": 1640, "protein_per_100g": 43, "name": "Whey"'
    )
    assert isinstance(info, LabelInfo)
    assert info.kj_per_100g == 1640.0
    assert info.protein_per_100g == 43.0


def test_repair_unterminated_json():
    # Closes missing object/array brackets and a dangling string/comma.
    assert repair_unterminated_json('{"a": 1, "b": 2') == '{"a": 1, "b": 2}'
    assert repair_unterminated_json('{"a": [1, 2') == '{"a": [1, 2]}'
    assert repair_unterminated_json('{"a": "unclosed') == '{"a": "unclosed"}'
    assert repair_unterminated_json('{"a": 1,') == '{"a": 1}'
    # Nothing to fix → None, so a balanced-but-otherwise-bad reply is untouched.
    assert repair_unterminated_json('{"a": 1}') is None
    assert repair_unterminated_json("no json here") is None
    # A "}" inside a string must not count as a real closer.
    assert repair_unterminated_json('{"a": "x}y"') == '{"a": "x}y"}'


def test_parse_estimate_string_numbers_and_negative_protein():
    est = parse_estimate('{"kcal": "1,050", "protein_g": -3, "name": "x"}')
    assert isinstance(est, MealEstimate)
    assert est.kcal == 1050.0
    assert est.protein_g is None  # negative → dropped


# ---- parse_label ------------------------------------------------------------

def test_parse_label_australian_panel():
    info = parse_label(
        '{"kj_per_100g": 1640, "kcal_per_100g": null, '
        '"protein_per_100g": 43, "serving_g": 30, "name": "Whey blend"}'
    )
    assert isinstance(info, LabelInfo)
    assert info.kj_per_100g == 1640.0
    assert info.kcal_per_100g is None
    assert info.protein_per_100g == 43.0
    assert info.serving_g == 30.0
    assert info.name == "Whey blend"
    assert info.has_energy


def test_parse_label_error_and_garbage():
    assert isinstance(parse_label('{"error": "blurry photo"}'), str)
    assert isinstance(parse_label("no json here"), str)
    # All-null values → nothing usable.
    assert isinstance(
        parse_label(
            '{"kj_per_100g": null, "kcal_per_100g": null, '
            '"protein_per_100g": null, "serving_g": null, "name": null}'
        ),
        str,
    )


def test_parse_label_negative_values_dropped():
    info = parse_label(
        '{"kj_per_100g": -100, "kcal_per_100g": null, '
        '"protein_per_100g": 20, "serving_g": null, "name": null}'
    )
    assert isinstance(info, LabelInfo)
    assert info.kj_per_100g is None       # negative dropped
    assert info.protein_per_100g == 20.0  # protein alone is still usable
    assert not info.has_energy


def test_parse_label_protein_only_is_usable():
    info = parse_label(
        '{"kj_per_100g": null, "protein_per_100g": 25.5, "name": "tuna"}'
    )
    assert isinstance(info, LabelInfo)
    assert info.protein_per_100g == 25.5
