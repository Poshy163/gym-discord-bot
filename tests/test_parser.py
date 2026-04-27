"""Parser smoke tests.

Covers the major parsing modes we care about not regressing: colon syntax,
free-form mentions, range notation, plate counts, BW+, plate-math, rep
capture, custom aliases, and the Epley 1RM helper. Each test maps 1:1 to a
documented input format from app/parser.py.
"""

from __future__ import annotations

import os

# Pin plate weight before importing the parser so PLATE_KG is deterministic.
os.environ.setdefault("PLATE_KG", "20")

from app.parser import estimated_one_rep_max, parse_message  # noqa: E402


def _by(eq: str, lifts):
    return [lift for lift in lifts if lift.equipment == eq]


def test_colon_syntax_kg():
    lifts = parse_message("Shoulder press: 31kg")
    assert len(lifts) == 1
    assert lifts[0].equipment == "shoulder press"
    assert lifts[0].weight_kg == 31
    assert lifts[0].confident is True


def test_freeform_mention():
    lifts = parse_message("Hit incline bench 70kg today, felt good")
    assert _by("incline bench press", lifts)


def test_range_takes_upper_bound():
    lifts = parse_message("Leg curls: 50 - 77 kg")
    assert lifts and lifts[0].weight_kg == 77


def test_plate_count_uses_env():
    lifts = parse_message("Squat: 3.5 plates")
    # 3.5 * PLATE_KG (20) = 70
    assert lifts and lifts[0].weight_kg == 70


def test_bodyweight_plus():
    lifts = parse_message("Dips: BW+20kg x5")
    assert lifts
    lift = lifts[0]
    assert lift.bodyweight_add is True
    assert lift.weight_kg == 20
    assert lift.reps == 5


def test_hip_machine_bare_weight_lines_are_known_equipment():
    add = parse_message("hip adduction 55kg")
    abd = parse_message("hip abductor 40kg")
    assert add and add[0].equipment == "hip adduction"
    assert add[0].weight_kg == 55
    assert abd and abd[0].equipment == "hip abduction"
    assert abd[0].weight_kg == 40


def test_plate_math_expression():
    lifts = parse_message("Bench: 2x20 + 10 kg")
    assert lifts and lifts[0].weight_kg == 50


def test_reps_capture_variants():
    a = parse_message("Bench: 100kg x5")
    b = parse_message("Squat: 100kg for 6 reps")
    c = parse_message("Deadlift: 100kg, 8 reps")
    assert a[0].reps == 5
    assert b[0].reps == 6
    assert c[0].reps == 8


def test_section_headers_are_not_lifts():
    # "Chest" alone should not produce a lift even with a number after.
    lifts = parse_message("Chest\nBench: 80kg")
    assert all(lift.equipment != "chest" for lift in lifts)


def test_skips_bodyweight_chatter_lines():
    lifts = parse_message("BW (Body Weight) - 67kg")
    assert lifts == []


def test_custom_alias_resolution():
    # Even though "wonky press" isn't a built-in alias, a custom mapping
    # should make it parse to the canonical.
    custom = {"wonky press": "shoulder press"}
    lifts = parse_message("Wonky press: 40kg", custom_aliases=custom)
    assert lifts and lifts[0].equipment == "shoulder press"


def test_custom_alias_freeform_resolution():
    custom = {"hack sled": "leg press"}
    lifts = parse_message("Hit 120kg on hack sled today", custom_aliases=custom)
    assert lifts
    assert lifts[0].equipment == "leg press"
    assert lifts[0].weight_kg == 120
    assert lifts[0].confident is True


def test_epley_one_rep_max():
    # Epley: 100 * (1 + 5/30) ≈ 116.67, rounded to 1 dp by the helper.
    assert estimated_one_rep_max(100, 5) == 116.7


def test_epley_caps_at_high_reps():
    assert estimated_one_rep_max(100, 20) is None


def test_epley_rejects_zero_reps():
    assert estimated_one_rep_max(100, 0) is None
    assert estimated_one_rep_max(0, 5) is None
