from __future__ import annotations

from datetime import timedelta, timezone

import pytest

from app.bodycomp import daily_mean_points, metric_summary


def test_metric_summary_sorts_and_reports_neutral_change():
    summary = metric_summary([
        ("2026-08-03T00:00:00+00:00", 20.5),
        ("2026-08-01T00:00:00+00:00", 21.2),
        ("2026-08-02T00:00:00+00:00", 20.9),
    ])
    assert summary is not None
    assert summary.first == 21.2
    assert summary.latest == 20.5
    assert summary.change == pytest.approx(-0.7)
    assert summary.low == 20.5
    assert summary.high == 21.2
    assert summary.samples == 3


def test_metric_summary_skips_bad_and_non_finite_rows():
    summary = metric_summary([
        ("not-a-date", 10),
        ("2026-08-01T00:00:00+00:00", float("nan")),
        ("2026-08-02T00:00:00+00:00", 18.4),
    ])
    assert summary is not None
    assert summary.first == summary.latest == 18.4
    assert metric_summary([("bad", float("inf"))]) is None


def test_daily_mean_points_uses_the_display_timezone_and_sorts():
    adelaide = timezone(timedelta(hours=9, minutes=30))
    points = daily_mean_points([
        # Both are 2 August locally.
        ("2026-08-01T15:00:00+00:00", 20.0),
        ("2026-08-02T01:00:00+00:00", 22.0),
        ("2026-07-31T14:00:00+00:00", 19.0),
    ], adelaide)
    assert [point.when.date().isoformat() for point in points] == [
        "2026-07-31", "2026-08-02",
    ]
    assert points[0].value == 19.0
    assert points[1].value == 21.0
    assert points[1].samples == 2


def test_daily_mean_points_accepts_naive_utc_and_empty_input():
    points = daily_mean_points(
        [("2026-08-01T12:00:00", 30.0)], timezone.utc,
    )
    assert len(points) == 1
    assert points[0].when.tzinfo is timezone.utc
    assert daily_mean_points([], timezone.utc) == []
