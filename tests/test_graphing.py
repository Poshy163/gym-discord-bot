from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.graphing import bodyweight_trend, daily_best_points, running_best_values


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


# --- trend smoothing ----------------------------------------------------------

def test_trend_values_is_a_trailing_mean_spanning_the_whole_series():
    from app.graphing import trend_values

    vals = [10.0, 20.0, 30.0, 40.0]
    out = trend_values(vals, window=3)
    assert len(out) == len(vals)          # never drops the leading points
    assert out[0] == 10.0                 # first averages one sample
    assert out[1] == 15.0                 # then two
    assert out[2] == 20.0                 # then a full window
    assert out[3] == 30.0


def test_trend_values_only_looks_backwards():
    """A centred window would let later weigh-ins bend the line you already
    saw; the last point must reflect only data that existed by then."""
    from app.graphing import trend_values

    early = trend_values([100.0, 102.0, 104.0], window=3)
    later = trend_values([100.0, 102.0, 104.0, 200.0], window=3)
    assert later[:3] == early             # appending never rewrites history


def test_trend_values_damps_a_single_spike():
    """The point of the line: one bad morning shouldn't read as the story."""
    from app.graphing import trend_values

    flat = [100.0, 100.0, 100.0, 110.0, 100.0, 100.0]
    out = trend_values(flat, window=3)
    assert max(out) < max(flat)           # the spike is pulled in
    assert max(out) == pytest.approx(103.3333, abs=1e-3)


def test_trend_values_handles_degenerate_input():
    from app.graphing import trend_values

    assert trend_values([]) == []
    assert trend_values([5.0]) == [5.0]
    assert trend_values([5.0, 7.0], window=0) == [5.0, 7.0]   # window clamps to 1


# --- bodyweight dashboard maths ---------------------------------------------


def _dated_weights(values: list[float], *, spacing_days: int = 1):
    start = datetime(2026, 5, 1, 12, tzinfo=timezone.utc)
    whens = [
        start + timedelta(days=index * spacing_days)
        for index in range(len(values))
    ]
    return whens, values


def test_bodyweight_trend_uses_elapsed_time_and_daily_grid():
    whens, values = _dated_weights([100.0, 98.0, 96.0], spacing_days=7)
    result = bodyweight_trend(whens, values, half_life_days=7)

    assert len(result.logged_trend_kg) == 3
    assert len(result.trend_when) == 15
    assert result.trend_when[0] == whens[0]
    assert result.trend_when[-1] == whens[-1]
    assert result.logged_trend_kg[-1] == pytest.approx(
        (100 * 0.25 + 98 * 0.5 + 96) / 1.75,
    )


def test_bodyweight_trend_projects_toward_a_saved_cutting_goal():
    whens, values = _dated_weights(
        [100.0 - index * 0.1 for index in range(30)],
    )
    result = bodyweight_trend(whens, values, goal_kg=95.0)

    assert result.goal_eta is not None
    assert result.projection_rate_kg_week == pytest.approx(-0.7)
    assert result.projection_when[0] == whens[-1]
    assert result.projection_kg[0] == pytest.approx(result.trend_kg[-1])
    assert result.projection_kg[-1] == 95.0


def test_bodyweight_trend_does_not_project_in_the_wrong_direction():
    whens, values = _dated_weights([100.0 + index * 0.1 for index in range(30)])
    result = bodyweight_trend(whens, values, goal_kg=95.0)

    assert result.goal_kg == 95.0
    assert result.goal_eta is None
    assert result.projection_when == []
    assert result.projection_kg == []


def test_bodyweight_trend_rate_is_fourteen_day_change_in_kg_per_week():
    whens, values = _dated_weights([100.0 - index * 0.1 for index in range(20)])
    result = bodyweight_trend(
        whens,
        values,
        half_life_days=0.01,
        rate_window_days=14,
    )

    assert result.rate_kg_week[:14] == [None] * 14
    assert result.rate_kg_week[14] == pytest.approx(-0.7, abs=1e-3)
