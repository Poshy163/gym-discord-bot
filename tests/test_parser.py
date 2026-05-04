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

from app.parser import (  # noqa: E402
    estimated_one_rep_max,
    parse_message,
    should_auto_store_lifts,
)


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
    assert lifts[0].structured is False


def test_conversational_sentence_is_not_auto_stored():
    lifts = parse_message(
        "You hit 295 kg on leg press and I swear we did 90 kg on calf raises"
    )
    assert _by("leg press", lifts)
    assert should_auto_store_lifts(lifts, min_lifts=2) is False


def test_structured_lift_line_is_auto_stored():
    lifts = parse_message("leg press 295kg")
    assert lifts and lifts[0].equipment == "leg press"
    assert lifts[0].structured is True
    assert should_auto_store_lifts(lifts, min_lifts=2) is True


def test_structured_stat_dump_is_auto_stored():
    lifts = parse_message("Bench press: 80kg\nSquat: 100kg")
    assert len(lifts) == 2
    assert should_auto_store_lifts(lifts, min_lifts=2) is True


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


def test_revo_equipment_bare_weight_lines_are_known_equipment():
    lifts = parse_message(
        "machine chest press 55kg\n"
        "stair master 20kg\n"
        "assault bike 10kg\n"
        "rowing machine 12kg\n"
        "ez bar 30kg\n"
        "kettlebell 24kg\n"
        "sled push 80kg\n"
        "med ball 8kg"
    )
    by_name = {lift.equipment: lift.weight_kg for lift in lifts}
    assert by_name["chest press"] == 55
    assert by_name["stairmaster"] == 20
    assert by_name["assault bike"] == 10
    assert by_name["rowing machine"] == 12
    assert by_name["ez bar"] == 30
    assert by_name["kettlebell"] == 24
    assert by_name["sled"] == 80
    assert by_name["medicine ball"] == 8


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


def test_skips_lines_containing_urls():
    # GIF / image links pasted in chat have digits in their query strings
    # that the weight extractor would otherwise read as absurd lifts.
    samples = [
        "https://tenor.com/view/lifting-gym-cool-12345.gif",
        "check this out https://example.com/clip.mp4?t=42",
        "www.youtube.com/watch?v=abc123",
    ]
    for text in samples:
        assert parse_message(text) == [], text


def test_strips_discord_mentions_and_emoji():
    # Snowflake IDs in mentions/emoji must not be read as weights.
    samples = [
        "<@123456789012345678> nice lift!",
        "<@!123456789012345678> hyped",
        "<@&987654321098765432> roll call",
        "<:flex:111122223333444455>",
        "<a:flex:111122223333444455> let's go",
    ]
    for text in samples:
        assert parse_message(text) == [], text


def test_strips_code_blocks_and_inline_code():
    # Pasted JSON/log lines often contain digits and colons. The fenced
    # block must not produce a "user_id" lift at 999...kg.
    fenced = "```json\n{\"user_id\": 123456789012345678, \"weight\": 999}\n```"
    assert parse_message(fenced) == []
    inline = "ran `bench: 9999999999` in the test harness"
    assert parse_message(inline) == []


def test_real_lift_alongside_mention_still_parses():
    # Stripping noise should not lose the actual lift on the same line.
    text = "<@123456789012345678> bench press: 80kg"
    lifts = parse_message(text)
    assert lifts and lifts[0].equipment == "bench press"
    assert lifts[0].weight_kg == 80


def test_parser_caps_absurd_weights():
    # Even if a heuristic produced a giant number, the parser must not
    # return it. 50000 plates would be ~1e6 kg -> dropped.
    lifts = parse_message("Bench: 50000 plates")
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
