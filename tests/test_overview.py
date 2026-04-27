from __future__ import annotations

from datetime import date, timezone

from app.overview import lift_overview


def test_lift_overview_summarises_consistency():
    overview = lift_overview(
        [
            ("2026-04-06T09:00:00+00:00", 100),
            ("2026-04-13T09:00:00+00:00", 105),
            ("2026-04-20T09:00:00+00:00", 110),
            ("2026-04-27T09:00:00+00:00", 120),
        ],
        timezone.utc,
        today=date(2026, 4, 27),
    )
    assert overview is not None
    assert overview.total_logs == 4
    assert overview.active_days == 4
    assert overview.active_weeks == 4
    assert overview.total_weeks == 4
    assert overview.current_week_streak == 4
    assert overview.consistency_score == 100
    assert overview.best_kg == 120
    assert overview.improvement_kg == 20
    assert overview.avg_gap_days == 7
    assert overview.longest_gap_days == 7


def test_lift_overview_counts_same_day_as_one_active_day():
    overview = lift_overview(
        [
            ("2026-04-27T09:00:00+00:00", 100),
            ("2026-04-27T10:00:00+00:00", 120),
        ],
        timezone.utc,
        today=date(2026, 4, 27),
    )
    assert overview is not None
    assert overview.total_logs == 2
    assert overview.active_days == 1
    assert overview.best_kg == 120
    assert overview.latest_kg == 120
    assert overview.avg_gap_days is None


def test_lift_overview_streak_drops_after_absence():
    overview = lift_overview(
        [
            ("2026-04-06T09:00:00+00:00", 100),
            ("2026-04-13T09:00:00+00:00", 105),
        ],
        timezone.utc,
        today=date(2026, 4, 27),
    )
    assert overview is not None
    assert overview.current_week_streak == 0
    assert overview.days_since_latest == 14
    assert overview.consistency_score < 100


def test_lift_overview_rejects_undatable_rows():
    assert lift_overview([("not-a-date", 100)], timezone.utc) is None