from __future__ import annotations

from datetime import timezone

from app.graphing import daily_best_points, running_best_values


def test_daily_best_points_collapses_same_local_day():
    points = daily_best_points(
        [
            ("2026-04-27T01:00:00+00:00", 175),
            ("2026-04-27T02:00:00+00:00", 275),
            ("2026-04-27T03:00:00+00:00", 290),
        ],
        timezone.utc,
    )
    assert len(points) == 1
    assert points[0].weight_kg == 290
    assert points[0].entries == 3


def test_daily_best_points_sorts_days():
    points = daily_best_points(
        [
            ("2026-04-28T01:00:00+00:00", 100),
            ("2026-04-27T01:00:00+00:00", 90),
        ],
        timezone.utc,
    )
    assert [point.weight_kg for point in points] == [90, 100]


def test_running_best_values_never_decreases():
    assert running_best_values([235, 175, 290, 275]) == [235, 235, 290, 290]
