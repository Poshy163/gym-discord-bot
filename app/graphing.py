"""Small helpers for turning lift history into graph-friendly points."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as dtime, timezone, tzinfo
from typing import Iterable


@dataclass(frozen=True)
class GraphPoint:
    when: datetime
    weight_kg: float
    entries: int


def daily_best_points(
    entries: Iterable[tuple[str, float]],
    display_tz: tzinfo,
) -> list[GraphPoint]:
    """Collapse raw lift rows into one best-weight point per local day."""
    grouped: dict[object, tuple[float, int]] = {}
    for logged_at, weight_kg in entries:
        try:
            dt = datetime.fromisoformat(logged_at)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local_day = dt.astimezone(display_tz).date()
        best, count = grouped.get(local_day, (float("-inf"), 0))
        grouped[local_day] = (max(best, float(weight_kg)), count + 1)

    points: list[GraphPoint] = []
    noon = dtime(hour=12)
    for local_day in sorted(grouped):
        best, count = grouped[local_day]
        points.append(
            GraphPoint(
                when=datetime.combine(local_day, noon, tzinfo=display_tz),
                weight_kg=best,
                entries=count,
            )
        )
    return points


def running_best_values(weights: Iterable[float]) -> list[float]:
    """Return the non-decreasing personal-best line for plotted weights."""
    bests: list[float] = []
    current = float("-inf")
    for weight in weights:
        current = max(current, float(weight))
        bests.append(current)
    return bests

