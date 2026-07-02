"""Tests for Open Food Facts response parsing (app/food_lookup.py).

Only the pure parsing layer is tested — the HTTP calls are thin wrappers
exercised in production. Fixtures mirror real OFF response shapes (values
often arrive as strings, brands as comma lists, nutriments partial).
"""
from __future__ import annotations

from app.food_lookup import FoodInfo, parse_product


def test_parse_product_full_record():
    info = parse_product({
        "code": "9300650658516",
        "product_name": "Vegemite",
        "brands": "Bega,Vegemite",
        "serving_quantity": "5",
        "nutriments": {
            "energy-kj_100g": 795,
            "energy-kcal_100g": 190,
            "proteins_100g": 24.7,
        },
    })
    assert isinstance(info, FoodInfo)
    assert info.name == "Vegemite"
    assert info.brand == "Bega"          # first of the comma list
    assert info.barcode == "9300650658516"
    assert info.kj_per_100g == 795.0
    assert info.kcal_per_100g == 190.0
    assert info.protein_per_100g == 24.7
    assert info.serving_g == 5.0
    assert info.has_energy


def test_parse_product_unitless_energy_is_kj():
    info = parse_product({
        "product_name": "Mystery bar",
        "nutriments": {"energy_100g": "1640"},
    })
    assert isinstance(info, FoodInfo)
    assert info.kj_per_100g == 1640.0
    assert info.kcal_per_100g is None
    assert info.has_energy


def test_parse_product_protein_only():
    info = parse_product({
        "product_name": "Tuna in springwater",
        "nutriments": {"proteins_100g": 25.5},
    })
    assert isinstance(info, FoodInfo)
    assert not info.has_energy
    assert info.protein_per_100g == 25.5


def test_parse_product_rejects_unusable():
    assert parse_product({}) is None                       # no name
    assert parse_product({"product_name": ""}) is None
    assert parse_product("not a dict") is None
    # Named but with zero usable nutriments.
    assert parse_product({"product_name": "Ghost", "nutriments": {}}) is None
    # Garbage values coerce to None and the record drops out.
    assert parse_product({
        "product_name": "Bad data",
        "nutriments": {"energy-kj_100g": "n/a"},
    }) is None


def test_parse_product_blank_optional_fields():
    info = parse_product({
        "product_name": "Plain oats",
        "brands": "",
        "code": "",
        "serving_quantity": "",
        "nutriments": {"energy-kj_100g": 1500},
    })
    assert isinstance(info, FoodInfo)
    assert info.brand is None
    assert info.barcode is None
    assert info.serving_g is None
