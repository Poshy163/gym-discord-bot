"""Pure helpers for body-composition history and trend preparation.

Home Assistant stores one row per metric per weigh-in.  This module turns those
rows into a small, presentation-agnostic series that Discord charts, the web
dashboard, and the AI coach can all consume without each surface inventing its
own timestamp and same-day rules.

Consumer impedance scales are noisy, so the helpers deliberately do not attach
meaning to a direction.  They report what changed; callers decide only how to
display it and should never label an increase/decrease as inherently good.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from datetime import time as dtime


@dataclass(frozen=True)
class MetricPoint:
    """One parsed body-composition value."""

    when: datetime
    value: float
    samples: int = 1


@dataclass(frozen=True)
class MetricSummary:
    """Headline statistics for a chronological metric series."""

    first: float
    latest: float
    change: float
    low: float
    high: float
    samples: int
    first_at: datetime
    latest_at: datetime


def _metric_points(
    entries: Iterable[tuple[str, float]],
) -> list[MetricPoint]:
    """Parse, validate, and chronologically sort stored metric rows."""

    points: list[MetricPoint] = []
    for recorded_at, raw_value in entries:
        try:
            when = datetime.fromisoformat(str(recorded_at).replace("Z", "+00:00"))
            value = float(raw_value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value):
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        points.append(MetricPoint(when=when, value=value))
    points.sort(key=lambda point: point.when)
    return points


def metric_summary(
    entries: Iterable[tuple[str, float]],
) -> MetricSummary | None:
    """Summarise a raw history, or return ``None`` when nothing is usable."""

    points = _metric_points(entries)
    if not points:
        return None
    values = [point.value for point in points]
    return MetricSummary(
        first=values[0],
        latest=values[-1],
        change=values[-1] - values[0],
        low=min(values),
        high=max(values),
        samples=len(values),
        first_at=points[0].when,
        latest_at=points[-1].when,
    )


def daily_mean_points(
    entries: Iterable[tuple[str, float]],
    display_tz: tzinfo,
) -> list[MetricPoint]:
    """Collapse readings to one mean value per local calendar day.

    A member can step on a scale more than once in a morning.  Treating those as
    separate x-axis points overstates that day's movement, while taking only the
    first or last makes the graph depend on timing.  The daily mean is stable and
    matches the existing bodyweight chart's rule.
    """

    grouped: dict[object, list[float]] = {}
    for point in _metric_points(entries):
        local_day = point.when.astimezone(display_tz).date()
        grouped.setdefault(local_day, []).append(point.value)

    noon = dtime(hour=12)
    return [
        MetricPoint(
            when=datetime.combine(day, noon, tzinfo=display_tz),
            value=sum(grouped[day]) / len(grouped[day]),
            samples=len(grouped[day]),
        )
        for day in sorted(grouped)
    ]
