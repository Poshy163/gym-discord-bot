"""Tests for equipment-name canonicalization, incl. Hevy's ``(Equipment)``
parenthetical stripping so Hevy imports merge with the matching machine."""

from __future__ import annotations

from app.aliases import canonicalize


def test_strips_hevy_equipment_parenthetical():
    # Hevy titles carry the equipment in parens; it must not fork a new machine.
    assert canonicalize("Bench Press (Barbell)") == "bench press"
    assert canonicalize("Lat Pulldown (Cable)") == "lat pulldown"
    assert canonicalize("Chest Press (Machine)") == "chest press"
    assert canonicalize("Incline Bench Press (Dumbbell)") == "incline bench press"
    assert canonicalize("Shoulder Press (Machine Plates)") == "shoulder press"
    # "Chest Fly" is an alias of the pec-dec machine.
    assert canonicalize("Chest Fly (Machine)") == "pec dec"


def test_unknown_exercise_stored_clean_without_parens():
    # Not in the alias table -> its own machine, but the parenthetical noise is
    # still stripped so it's stored consistently.
    assert canonicalize("Straight Arm Lat Pulldown (Cable)") == "straight arm lat pulldown"
    assert canonicalize("Standing Calf Raise (Machine)") == "standing calf raise"
    # No parenthetical, no alias -> unchanged clean form.
    assert canonicalize("T Bar Row") == "t bar row"


def test_plain_aliases_still_resolve():
    assert canonicalize("bench") == "bench press"
    assert canonicalize("OHP") == "shoulder press"
    assert canonicalize("triceps pushdown") == "tricep pushdown"


def test_empty_and_none_safe():
    assert canonicalize("") == ""
    assert canonicalize("()") == ""
