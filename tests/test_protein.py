"""Tests for protein parsing/formatting helpers and DB methods."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app import protein
from app.db import Database


# ---------------------------------------------------------------------------
# Parsing / formatting
# ---------------------------------------------------------------------------

def test_parse_protein_amount():
    assert protein.parse_protein_amount("180") == 180.0
    assert protein.parse_protein_amount("180g") == 180.0
    assert protein.parse_protein_amount("40 g protein") == 40.0
    assert protein.parse_protein_amount("12.5") == 12.5
    assert protein.parse_protein_amount("abc") is None
    assert protein.parse_protein_amount("") is None
    # Multiplier prefix: 70g of a 43 g/100g food.
    assert protein.parse_protein_amount("0.7x43") == pytest.approx(30.1)
    assert protein.parse_protein_amount("0.7 x 43g") == pytest.approx(30.1)


def test_parse_protein_chat_message_accepts_marked_amounts():
    assert protein.parse_protein_chat_message("40p") == 40.0
    assert protein.parse_protein_chat_message("40 p") == 40.0
    assert protein.parse_protein_chat_message("40g protein") == 40.0
    assert protein.parse_protein_chat_message("40 protein") == 40.0
    assert protein.parse_protein_chat_message("protein 40") == 40.0
    assert protein.parse_protein_chat_message("protein 40g") == 40.0
    assert protein.parse_protein_chat_message("40p!") == 40.0


def test_parse_protein_chat_message_multiplier():
    # Per-100g label maths: 70g of a 43 g/100g food.
    assert protein.parse_protein_chat_message("0.7x43p") == pytest.approx(30.1)
    assert protein.parse_protein_chat_message("0.7 x 43 p") == pytest.approx(30.1)
    assert protein.parse_protein_chat_message("0.7*43g protein") == pytest.approx(30.1)
    assert protein.parse_protein_chat_message("2x20p") == 40.0
    assert protein.parse_protein_chat_message("protein 0.7x43") == pytest.approx(30.1)
    # Still needs the protein marker — a multiplied bare number is not a log.
    assert protein.parse_protein_chat_message("0.7x43") is None
    assert protein.parse_protein_chat_message("0.7x43g") is None


def test_parse_protein_chat_message_rejects_ambiguous():
    # A bare number or weight must NOT be read as protein.
    assert protein.parse_protein_chat_message("40") is None
    assert protein.parse_protein_chat_message("40g") is None
    assert protein.parse_protein_chat_message("bench 40kg") is None
    assert protein.parse_protein_chat_message("had 40p of protein today") is None
    assert protein.parse_protein_chat_message("") is None


def test_format_grams():
    assert protein.format_grams(40) == "40 g"
    assert protein.format_grams(39.6) == "40 g"
    assert protein.format_grams(0) == "0 g"


# ---------------------------------------------------------------------------
# DB layer
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    d = Database(tmp_path / "gym.sqlite3")
    yield d
    d.close()


def test_protein_update_last_edits_most_recent(db):
    db.protein_add(1, 100, "Alice", 40)
    db.protein_add(1, 100, "Alice", 25)  # most recent
    old = db.protein_update_last(1, 100, 55, username="Alice")
    assert old["grams"] == 25
    rows = db.protein_entries_between(
        1, 100, "2000-01-01T00:00:00+00:00", "2100-01-01T00:00:00+00:00",
    )
    assert sorted(r["grams"] for r in rows) == [40, 55]
    assert db.protein_update_last(1, 999, 30) is None


def test_protein_goal_set_get_remove(db):
    assert db.protein_goal_get(1, 100) is None
    db.protein_goal_set(1, 100, "alice", 180)
    row = db.protein_goal_get(1, 100)
    assert row["daily_target_g"] == 180.0
    assert row["username"] == "alice"
    # Update is upsert.
    db.protein_goal_set(1, 100, "alice", 200)
    assert db.protein_goal_get(1, 100)["daily_target_g"] == 200.0
    # tracked_users matches goals against current guild membership.
    db.upsert_member(1, 100, "alice", "Alice")
    assert {r["user_id"] for r in db.protein_tracked_users(1)} == {100}
    assert db.protein_goal_remove(1, 100) is True
    assert db.protein_goal_get(1, 100) is None
    assert db.protein_goal_remove(1, 100) is False


def test_protein_weekend_override_and_independence(db):
    from datetime import timedelta

    from app import targets

    today = targets.local_today()
    days = [today + timedelta(days=n) for n in range(7)]
    weekday = next(d for d in days if not targets.is_weekend(d))
    weekend = next(d for d in days if targets.is_weekend(d))

    db.calorie_goal_set(1, 100, "alice", 1500)
    db.protein_goal_set(1, 100, "alice", 180, 200)
    assert db.protein_goal_get(1, 100, weekday)["daily_target_g"] == 180
    assert db.protein_goal_get(1, 100, weekend)["daily_target_g"] == 200
    # Calories have no weekend rule, so they stay flat across the split.
    assert db.calorie_goal_get(1, 100, weekend)["daily_target_kcal"] == 1500

    # Turning protein off must not disturb the calorie tracker, and must clear
    # the weekend ceiling too.
    assert db.protein_goal_remove(1, 100) is True
    assert db.protein_goal_get(1, 100, weekday) is None
    assert db.protein_goal_get(1, 100, weekend) is None
    assert db.calorie_goal_get(1, 100)["daily_target_kcal"] == 1500


def test_protein_goal_and_total_are_global_per_user(db):
    db.protein_goal_set(1, 100, "alice", 180)
    # Goal resolves from any server / DM.
    assert db.protein_goal_get(2, 100)["daily_target_g"] == 180.0
    assert db.protein_goal_get(999, 100)["daily_target_g"] == 180.0
    # Re-setting elsewhere consolidates to one row.
    db.protein_goal_set(2, 100, "alice", 200)
    assert db.protein_goal_get(1, 100)["daily_target_g"] == 200.0
    # Totals aggregate across servers.
    db.protein_add(1, 100, "alice", 40, message_id=1)
    db.protein_add(2, 100, "alice", 30, message_id=2)
    total, n = db.protein_total_between(
        1, 100, "2000-01-01T00:00:00+00:00", "2100-01-01T00:00:00+00:00",
    )
    assert total == 70.0 and n == 2
    # Removing stops tracking everywhere.
    assert db.protein_goal_remove(2, 100) is True
    assert db.protein_goal_get(1, 100) is None


def test_protein_add_total_and_dedupe(db):
    db.protein_add(1, 100, "alice", 40, message_id=10)
    db.protein_add(1, 100, "alice", 30, message_id=11)
    # Duplicate message_id is ignored.
    assert db.protein_add(1, 100, "alice", 99, message_id=10) == 0
    total, n = db.protein_total_between(
        1, 100, "2000-01-01T00:00:00+00:00", "2100-01-01T00:00:00+00:00",
    )
    assert total == 70.0 and n == 2


def test_protein_pop_last(db):
    db.protein_add(
        1, 100, "alice", 40, note="chicken",
        logged_at=datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc),
    )
    db.protein_add(
        1, 100, "alice", 30, note="shake",
        logged_at=datetime(2026, 6, 1, 19, 0, tzinfo=timezone.utc),
    )
    popped = db.protein_pop_last(1, 100)
    assert popped["grams"] == 30 and popped["note"] == "shake"
    remaining = db.protein_entries_between(
        1, 100, "2000-01-01T00:00:00+00:00", "2100-01-01T00:00:00+00:00",
    )
    assert [r["grams"] for r in remaining] == [40]
    db.protein_pop_last(1, 100)
    assert db.protein_pop_last(1, 100) is None


def test_protein_entries_between_scoped_to_user(db):
    db.protein_add(1, 100, "alice", 40)
    db.protein_add(1, 200, "bob", 50)
    rows = db.protein_entries_between(
        1, 100, "2000-01-01T00:00:00+00:00", "2100-01-01T00:00:00+00:00",
    )
    assert [r["grams"] for r in rows] == [40]


def test_get_and_delete_protein_entry_by_message(db):
    # Backs ❌ reaction-undo on protein/combined replies.
    eid = db.protein_add(1, 100, "alice", 40, message_id=777)
    row = db.get_protein_entry_by_message(1, 777)
    assert row is not None and row["id"] == eid and row["user_id"] == 100
    assert db.get_protein_entry_by_message(1, 999) is None
    # Wrong user can't delete; correct (guild, user) removes it.
    assert db.delete_protein_entry(1, 999, eid) is None
    removed = db.delete_protein_entry(1, 100, eid)
    assert removed is not None and removed["grams"] == 40
    assert db.delete_protein_entry(1, 100, eid) is None


# ---------------------------------------------------------------------------
# Bodyweight-linked protein target (protein_bw_links + set_bodyweight)
# ---------------------------------------------------------------------------

def _week_days():
    """(today, a_weekday, a_weekend_day) within the next 7 days — so each is on
    or after today and resolves against the rules effective now."""
    from datetime import timedelta

    from app import targets

    today = targets.local_today()
    days = [today + timedelta(days=n) for n in range(7)]
    weekday = next(d for d in days if not targets.is_weekend(d))
    weekend = next(d for d in days if targets.is_weekend(d))
    return today, weekday, weekend


def test_protein_bw_link_crud(db):
    assert db.protein_bw_link_get(100) is None
    db.protein_bw_link_set(100, "alice")
    link = db.protein_bw_link_get(100)
    assert link["grams_per_kg"] == 1.0 and link["username"] == "alice"
    # Upsert keeps one row and can carry a future non-1.0 ratio.
    db.protein_bw_link_set(100, "alice", 1.6)
    assert db.protein_bw_link_get(100)["grams_per_kg"] == 1.6
    assert db.protein_bw_link_remove(100) is True
    assert db.protein_bw_link_get(100) is None
    assert db.protein_bw_link_remove(100) is False


def test_set_bodyweight_without_link_leaves_protein_untouched(db):
    # No link → a weigh-in must not invent a protein target, and returns None.
    assert db.set_bodyweight(1, 100, 82.0) is None
    assert db.protein_goal_get(1, 100) is None


def test_bodyweight_link_updates_protein_on_change_but_not_on_same_weight(db):
    _today, weekday, weekend = _week_days()
    # Link at 82 kg the way /protein setup target:bodyweight does.
    db.set_bodyweight(1, 100, 82.0)
    db.protein_bw_link_set(100, "alice")
    db.protein_goal_set(1, 100, "alice", round(82.0))
    assert db.protein_goal_get(1, 100, weekday)["daily_target_g"] == 82
    assert db.protein_goal_get(1, 100, weekend)["daily_target_g"] == 82

    # Re-logging the same weight writes nothing new and reports no change.
    assert db.set_bodyweight(1, 100, 82.0) is None
    defaults = [
        r for r in db.nutrition_target_rows(100)
        if r["macro"] == "protein_g" and r["scope"] == "default"
    ]
    assert len(defaults) == 1

    # A real change moves it on every day and returns the new gram count.
    assert db.set_bodyweight(1, 100, 84.3) == 84   # round(84.3)
    assert db.protein_goal_get(1, 100, weekday)["daily_target_g"] == 84
    assert db.protein_goal_get(1, 100, weekend)["daily_target_g"] == 84


def test_bodyweight_link_preserves_history_on_change(db):
    from datetime import timedelta

    from app import targets

    db.set_bodyweight(1, 100, 82.0)
    db.protein_bw_link_set(100, "alice")
    db.protein_goal_set(1, 100, "alice", 82)   # effective from beginning of time
    db.set_bodyweight(1, 100, 90.0)            # bumps it, effective today
    today = targets.local_today()
    assert db.protein_goal_get(1, 100, today)["daily_target_g"] == 90
    # A day already lived through still resolves against the old ceiling.
    yesterday = today - timedelta(days=1)
    assert db.protein_goal_get(1, 100, yesterday)["daily_target_g"] == 82


def test_bodyweight_link_neutralizes_weekend_override(db):
    _today, weekday, weekend = _week_days()
    # User first runs a weekday/weekend protein split.
    db.protein_goal_set(1, 100, "alice", 180, 220)
    assert db.protein_goal_get(1, 100, weekend)["daily_target_g"] == 220
    # /protein setup target:bodyweight at 82 kg: link, derive the default, and
    # clear the weekend override (weekend_g=None) exactly as the command does.
    db.protein_bw_link_set(100, "alice")
    db.protein_goal_set(1, 100, "alice", round(82.0), None)
    assert db.protein_goal_get(1, 100, weekday)["daily_target_g"] == 82
    assert db.protein_goal_get(1, 100, weekend)["daily_target_g"] == 82
    assert db.protein_goal_get(1, 100, weekend)["split"] is False
    # A later weigh-in keeps every day on the derived number.
    assert db.set_bodyweight(1, 100, 85.0) == 85
    assert db.protein_goal_get(1, 100, weekend)["daily_target_g"] == 85


def test_bodyweight_link_honors_grams_per_kg_ratio(db):
    # Storing the ratio lets a future cut use 1.6 g/kg with no schema change.
    db.set_bodyweight(1, 100, 80.0)
    db.protein_bw_link_set(100, "alice", 1.6)
    db.protein_goal_set(1, 100, "alice", 100)   # a starting ceiling to move off
    assert db.set_bodyweight(1, 100, 80.0) == 128   # round(80 * 1.6)
    assert db.protein_goal_get(1, 100)["daily_target_g"] == 128


def test_bodyweight_link_ignores_absurd_derived_target(db):
    # A weigh-in past the derived-target backstop is still recorded, but must
    # not push an absurd ceiling into the targets table.
    db.set_bodyweight(1, 100, 82.0)
    db.protein_bw_link_set(100, "alice")
    db.protein_goal_set(1, 100, "alice", 82)
    assert db.set_bodyweight(1, 100, 600.0) is None       # 600 g > backstop
    assert db.get_latest_bodyweight(1, 100)["weight_kg"] == 600.0
    assert db.protein_goal_get(1, 100)["daily_target_g"] == 82   # unchanged


def test_unlinking_stops_bodyweight_from_moving_protein(db):
    db.set_bodyweight(1, 100, 82.0)
    db.protein_bw_link_set(100, "alice")
    db.protein_goal_set(1, 100, "alice", 82)
    assert db.protein_bw_link_remove(100) is True
    # With the link gone a new weigh-in is inert for protein.
    assert db.set_bodyweight(1, 100, 95.0) is None
    assert db.protein_goal_get(1, 100)["daily_target_g"] == 82


def test_bodyweight_link_update_keeps_home_guild(db):
    # /protein setup target:bodyweight in Guild A files the targets under A.
    db.set_bodyweight(1, 100, 82.0)
    db.protein_bw_link_set(100, "alice")
    db.protein_goal_set(1, 100, "alice", round(82.0))
    assert db.nutrition_home_guild(100) == 1
    # A weigh-in typed in Guild B moves the derived number, but a weigh-in is
    # not a nutrition command: it must not re-home the user's global targets to
    # whichever server the scale reading happened in.
    assert db.set_bodyweight(2, 100, 90.0) == 90
    assert db.protein_goal_get(1, 100)["daily_target_g"] == 90
    assert db.nutrition_home_guild(100) == 1


def test_web_protein_off_toggle_unlinks_from_bodyweight(db):
    # /protein setup target:bodyweight at 82 kg.
    db.set_bodyweight(1, 100, 82.0)
    db.protein_bw_link_set(100, "alice")
    db.protein_goal_set(1, 100, "alice", 82)
    # The web dashboard turns protein tracking off (blank weekday). Tombstoning
    # the target isn't enough — the link has to break too, or the next weigh-in
    # silently re-arms the tracker the user just switched off.
    db.web_nutrition_targets_set(
        1, 100, "alice", kcal=2000, weekend_kcal=None,
        protein_g=None, weekend_protein_g=None, actor_name="admin",
    )
    assert db.protein_bw_link_get(100) is None
    assert db.protein_goal_get(1, 100) is None
    assert db.set_bodyweight(1, 100, 90.0) is None
    assert db.protein_goal_get(1, 100) is None


def test_web_protein_fixed_number_unlinks_from_bodyweight(db):
    # A fixed protein number set on the web, while linked, must break the link
    # so a later weigh-in can't silently overwrite the chosen number — matching
    # Discord's `/protein setup <n>`.
    db.set_bodyweight(1, 100, 82.0)
    db.protein_bw_link_set(100, "alice")
    db.protein_goal_set(1, 100, "alice", 82)
    db.web_nutrition_targets_set(
        1, 100, "alice", kcal=None, weekend_kcal=None,
        protein_g=150, weekend_protein_g=None, actor_name="admin",
    )
    assert db.protein_bw_link_get(100) is None
    assert db.protein_goal_get(1, 100)["daily_target_g"] == 150
    assert db.set_bodyweight(1, 100, 90.0) is None
    assert db.protein_goal_get(1, 100)["daily_target_g"] == 150
