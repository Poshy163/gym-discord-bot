"""Tests for the per-day nutrition target resolver.

The rules that matter here are the ones a user would notice: a single goal keeps
applying seven days a week, a weekend override only touches Saturday and Sunday,
and editing a goal never re-scores a day that has already happened.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.targets import (
    BEGINNING_OF_TIME,
    MACRO_KCAL,
    MACRO_PROTEIN,
    SCOPE_DEFAULT,
    SCOPE_WEEKEND,
    WEEKDAY_LABEL,
    WEEKEND_LABEL,
    band,
    band_stats,
    is_weekend,
    local_day_of,
    mean_target,
    resolve,
    resolve_days,
    scope_matches,
    scope_priority,
)

# 2026-07-06 is a Monday, so ..11 is Saturday and ..12 is Sunday.
MON = date(2026, 7, 6)
FRI = date(2026, 7, 10)
SAT = date(2026, 7, 11)
SUN = date(2026, 7, 12)
WEEK = [date(2026, 7, 6 + n) for n in range(7)]


def row(macro, scope, value, effective_from=BEGINNING_OF_TIME, set_at="2026-01-01T00:00:00+00:00"):
    return {
        "macro": macro, "scope": scope, "value": value,
        "effective_from": effective_from, "set_at": set_at,
    }


def kcal_only(value=1500):
    return [row(MACRO_KCAL, SCOPE_DEFAULT, value)]


def split_rows(weekday=1500, weekend=2200, protein=180):
    """The example config: 1500/2200 kcal, 180 g protein every day."""
    return [
        row(MACRO_KCAL, SCOPE_DEFAULT, weekday),
        row(MACRO_KCAL, SCOPE_WEEKEND, weekend),
        row(MACRO_PROTEIN, SCOPE_DEFAULT, protein),
    ]


# ---- scope primitives ------------------------------------------------------

def test_is_weekend_and_band():
    assert not is_weekend(FRI) and band(FRI) == "weekday"
    assert is_weekend(SAT) and is_weekend(SUN)
    assert band(SAT) == "weekend"


@pytest.mark.parametrize("scope,day,expected", [
    (SCOPE_DEFAULT, MON, True),
    (SCOPE_DEFAULT, SAT, True),
    (SCOPE_WEEKEND, SAT, True),
    (SCOPE_WEEKEND, MON, False),
    ("weekday", MON, True),
    ("weekday", SAT, False),
    ("dow:0", MON, True),      # Monday
    ("dow:2", MON, False),
    ("dow:9", MON, False),     # out of range
    ("dow:x", MON, False),     # unparseable
    ("date:2026-07-06", MON, True),
    ("date:2026-07-07", MON, False),
])
def test_scope_matches(scope, day, expected):
    assert scope_matches(scope, day) is expected


def test_unknown_scope_never_matches_and_sorts_last():
    # A scope written by a newer build must be skipped, not crash an old one.
    assert scope_matches("tag:training", MON) is False
    assert scope_priority("tag:training") == -1


def test_scope_priority_orders_general_to_specific():
    assert (
        scope_priority(SCOPE_DEFAULT)
        < scope_priority(SCOPE_WEEKEND)
        < scope_priority("dow:2")
        < scope_priority("date:2026-12-25")
    )


# ---- backward compatibility: one goal, seven days ---------------------------

def test_single_default_goal_applies_every_day():
    rows = kcal_only(1500) + [row(MACRO_PROTEIN, SCOPE_DEFAULT, 180)]
    for day in WEEK:
        r = resolve(rows, day)
        assert r.kcal.value == 1500
        assert r.protein.value == 180
        # No split => no "which set am I on" banner anywhere.
        assert r.split is False
        assert r.label is None


def test_no_rows_means_not_tracking():
    r = resolve([], MON)
    assert r.kcal.value is None and r.protein.value is None
    assert r.label is None


# ---- the feature: weekday vs weekend ---------------------------------------

def test_weekend_override_applies_only_at_the_weekend():
    rows = split_rows()
    for day in (MON, FRI):
        r = resolve(rows, day)
        assert r.kcal.value == 1500
        assert r.label == WEEKDAY_LABEL
    for day in (SAT, SUN):
        r = resolve(rows, day)
        assert r.kcal.value == 2200
        assert r.kcal.scope == SCOPE_WEEKEND
        assert r.label == WEEKEND_LABEL


def test_macros_fall_through_independently():
    # Calories have a weekend override; protein doesn't, so protein keeps using
    # the all-week number even on Saturday.
    r = resolve(split_rows(), SAT)
    assert r.kcal.value == 2200 and r.kcal.scope == SCOPE_WEEKEND
    assert r.protein.value == 180 and r.protein.scope == SCOPE_DEFAULT
    assert r.protein.split is False


def test_label_for_a_single_macro_ignores_the_other_macros_split():
    # A protein-only reply shouldn't announce "Using Weekend Targets" just
    # because this user's *calories* differ at the weekend.
    r = resolve(split_rows(), SAT)
    assert r.label == WEEKEND_LABEL                     # both macros on show
    assert r.label_for(MACRO_KCAL) == WEEKEND_LABEL     # calories do split
    assert r.label_for(MACRO_PROTEIN) is None           # protein doesn't


def test_protein_only_user_has_no_calorie_target():
    r = resolve([row(MACRO_PROTEIN, SCOPE_DEFAULT, 180)], SAT)
    assert r.kcal.value is None
    assert r.protein.value == 180


# ---- historical data: editing a goal never re-scores the past ---------------

def test_editing_a_goal_leaves_earlier_days_alone():
    rows = kcal_only(1500) + [
        row(MACRO_KCAL, SCOPE_DEFAULT, 1600, effective_from="2026-07-08"),
    ]
    assert resolve(rows, date(2026, 7, 7)).kcal.value == 1500  # before the edit
    assert resolve(rows, date(2026, 7, 8)).kcal.value == 1600  # the day of
    assert resolve(rows, date(2026, 7, 9)).kcal.value == 1600  # after


def test_adding_a_weekend_target_does_not_rewrite_past_weekends():
    rows = kcal_only(1500) + [
        row(MACRO_KCAL, SCOPE_WEEKEND, 2200, effective_from="2026-07-09"),
    ]
    assert resolve(rows, date(2026, 7, 4)).kcal.value == 1500   # Sat, before
    assert resolve(rows, SAT).kcal.value == 2200                # Sat, after


def test_future_dated_rules_are_ignored_until_they_arrive():
    rows = kcal_only(1500) + [
        row(MACRO_KCAL, SCOPE_DEFAULT, 1800, effective_from="2026-08-01"),
    ]
    assert resolve(rows, MON).kcal.value == 1500
    assert resolve(rows, date(2026, 8, 2)).kcal.value == 1800


def test_same_day_edits_break_ties_on_set_at():
    rows = [
        row(MACRO_KCAL, SCOPE_DEFAULT, 1500, "2026-07-06", "2026-07-06T01:00:00+00:00"),
        row(MACRO_KCAL, SCOPE_DEFAULT, 1700, "2026-07-06", "2026-07-06T09:00:00+00:00"),
    ]
    assert resolve(rows, MON).kcal.value == 1700


# ---- clearing an override (the NULL tombstone) -----------------------------

def test_null_valued_rule_clears_an_override_without_touching_history():
    rows = split_rows() + [
        row(MACRO_KCAL, SCOPE_WEEKEND, None, effective_from="2026-07-09"),
    ]
    # Saturdays now fall back to the all-week number, and the UI goes quiet.
    now = resolve(rows, SAT)
    assert now.kcal.value == 1500
    assert now.kcal.split is False
    assert now.label is None
    # The Saturday before the change still knows it was aiming at 2,200.
    assert resolve(rows, date(2026, 7, 4)).kcal.value == 2200


def test_tombstoning_every_scope_turns_a_tracker_off():
    rows = split_rows() + [
        row(MACRO_KCAL, SCOPE_DEFAULT, None, effective_from="2026-07-09"),
        row(MACRO_KCAL, SCOPE_WEEKEND, None, effective_from="2026-07-09"),
    ]
    off = resolve(rows, SAT)
    assert off.kcal.value is None
    assert off.protein.value == 180        # the other tracker is untouched
    # A day before the opt-out still resolves, so old reports stay readable.
    assert resolve(rows, date(2026, 7, 4)).kcal.value == 2200


def test_nulling_only_the_default_leaves_the_weekend_override_winning():
    # Why _nutrition_tracking_off has to tombstone every scope, not just default:
    # weekend outranks default, so a Saturday would keep its target.
    rows = split_rows() + [
        row(MACRO_KCAL, SCOPE_DEFAULT, None, effective_from="2026-07-09"),
    ]
    next_monday = date(2026, 7, 13)
    assert resolve(rows, next_monday).kcal.value is None
    assert resolve(rows, SAT).kcal.value == 2200


# ---- bodyweight-linked protein resolves every day --------------------------

# 2026-07-08 is a Wednesday — a plain weekday to contrast with SAT/SUN.
WED = date(2026, 7, 8)


def test_bodyweight_linked_protein_resolves_on_weekday_and_weekend():
    # A bodyweight link writes one default-scope protein row (round(kg) grams).
    # It has to resolve to the same number on every day, so verify a Wednesday
    # and a Saturday directly rather than trusting the setup flow.
    rows = [row(MACRO_PROTEIN, SCOPE_DEFAULT, 82)]
    assert resolve(rows, WED).protein.value == 82
    assert resolve(rows, SAT).protein.value == 82
    assert resolve(rows, SAT).protein.split is False  # no weekend banner


def test_bodyweight_link_tombstones_a_live_weekend_override():
    # Linking neutralises an existing weekend protein override with a NULL
    # weekend row so the derived number wins on Saturdays too — without deleting
    # the history the old weekends were scored against.
    rows = [
        row(MACRO_PROTEIN, SCOPE_DEFAULT, 200, effective_from="2026-07-01"),
        row(MACRO_PROTEIN, SCOPE_WEEKEND, 220, effective_from="2026-07-01"),
        # The link fires on the 6th: a fresh default plus a weekend tombstone.
        row(MACRO_PROTEIN, SCOPE_DEFAULT, 82, effective_from="2026-07-06"),
        row(MACRO_PROTEIN, SCOPE_WEEKEND, None, effective_from="2026-07-06"),
    ]
    assert resolve(rows, WED).protein.value == 82
    assert resolve(rows, SAT).protein.value == 82      # override cleared
    assert resolve(rows, SAT).protein.split is False
    # A Saturday before the link still remembers it was aiming at 220.
    assert resolve(rows, date(2026, 7, 4)).protein.value == 220


# ---- extensibility ---------------------------------------------------------

def test_more_specific_scopes_win():
    rows = kcal_only(1500) + [
        row(MACRO_KCAL, SCOPE_WEEKEND, 2200),
        row(MACRO_KCAL, "dow:2", 1800),               # Wednesdays
        row(MACRO_KCAL, "date:2026-07-11", 3000),     # this one Saturday
    ]
    assert resolve(rows, date(2026, 7, 8)).kcal.value == 1800   # Wednesday
    assert resolve(rows, SAT).kcal.value == 3000                # beats weekend
    assert resolve(rows, SUN).kcal.value == 2200


def test_unrecognised_scope_is_skipped_not_fatal():
    rows = kcal_only(1500) + [row(MACRO_KCAL, "tag:training", 3000)]
    assert resolve(rows, MON).kcal.value == 1500


# ---- analytics -------------------------------------------------------------

def test_mean_target_averages_the_week_a_split_actually_produces():
    # What /calories tdee needs: (5 x 1500 + 2 x 2200) / 7.
    assert mean_target(split_rows(), WEEK) == pytest.approx((5 * 1500 + 2 * 2200) / 7)
    assert mean_target(split_rows(), WEEK, MACRO_PROTEIN) == 180
    assert mean_target([], WEEK) is None


def test_resolve_days_covers_the_requested_range():
    got = resolve_days(split_rows(), WEEK)
    assert set(got) == set(WEEK)
    assert got[SAT].kcal.value == 2200


def test_band_stats_scores_each_day_against_its_own_target():
    rows = split_rows()
    intake = {MON: 1500.0, FRI: 1800.0, SAT: 1100.0}
    stats = band_stats(intake, resolve_days(rows, WEEK), MACRO_KCAL)
    assert stats["weekday"].days == 2
    assert stats["weekday"].avg_intake == pytest.approx(1650)
    assert stats["weekday"].avg_target == 1500
    assert stats["weekday"].adherence == pytest.approx((1500 / 1500 + 1800 / 1500) / 2)
    # The Saturday is judged against 2,200 — half of it, not 73% of 1,500.
    assert stats["weekend"].days == 1
    assert stats["weekend"].avg_target == 2200
    assert stats["weekend"].adherence == pytest.approx(0.5)


def test_band_stats_treats_missing_days_as_gaps_not_zeroes():
    stats = band_stats({MON: 1500.0}, resolve_days(split_rows(), WEEK), MACRO_KCAL)
    assert stats["weekday"].days == 1
    assert "weekend" not in stats  # nothing logged, so no bucket at all


def test_band_stats_without_a_target_reports_intake_only():
    rows = [row(MACRO_PROTEIN, SCOPE_DEFAULT, 180)]
    stats = band_stats({MON: 900.0}, resolve_days(rows, WEEK), MACRO_KCAL)
    assert stats["weekday"].avg_intake == 900
    assert stats["weekday"].avg_target is None
    assert stats["weekday"].adherence is None


# ---- local-day mapping -----------------------------------------------------

def test_local_day_of_uses_the_display_timezone():
    # A naive timestamp is read as UTC.
    dt = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)
    assert local_day_of(dt) in (date(2026, 7, 11), date(2026, 7, 12))
    assert local_day_of(None) is not None


def test_local_day_of_maps_a_backdated_entry_to_its_own_day():
    sat_noon = datetime(2026, 7, 11, 2, 30, tzinfo=timezone.utc)
    day = local_day_of(sat_noon)
    # Whatever the configured zone, the entry lands on one definite local day,
    # and that day is what resolve() gets asked about.
    assert resolve(split_rows(), day).kcal.value in (1500, 2200)
