"""Small helpers for turning lift history into graph-friendly points."""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from datetime import time as dtime


@dataclass(frozen=True)
class GraphPoint:
    when: datetime
    weight_kg: float
    entries: int


@dataclass(frozen=True)
class BodyweightTrend:
    """Presentation-ready bodyweight trend and goal projection.

    All calculations are deliberately kept out of the Matplotlib renderer so
    the chart maths is testable without a plotting backend. ``trend_when`` is a
    daily grid, while ``logged_trend_kg`` lines up with the original logged
    dates and is used to estimate day-to-day scale noise.
    """

    logged_when: list[datetime]
    logged_kg: list[float]
    logged_trend_kg: list[float]
    trend_when: list[datetime]
    trend_kg: list[float]
    rate_kg_week: list[float | None]
    noise_sd_kg: float
    projection_when: list[datetime]
    projection_kg: list[float]
    goal_kg: float | None
    goal_eta: date | None
    projection_rate_kg_week: float | None


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


def trend_values(values: Iterable[float], window: int = 3) -> list[float]:
    """Trailing mean over the last ``window`` points, one output per input.

    Bodyweight is noisy — hydration, time of day and what you last ate move it
    a kilo either way — so a raw line makes a single bad morning look like the
    story. Smoothing lets the actual trajectory read while the real readings
    stay on the chart underneath.

    Trailing rather than centred so the last point reflects only data that
    existed by then; a centred window would let future weigh-ins bend the line
    you already saw. The first points average fewer samples rather than being
    dropped, so the line spans the whole series.
    """
    out: list[float] = []
    seen: list[float] = []
    span = max(1, int(window))
    for value in values:
        seen.append(float(value))
        chunk = seen[-span:]
        out.append(sum(chunk) / len(chunk))
    return out


def _time_aware_ewma(
    whens: list[datetime],
    values: list[float],
    half_life_days: float,
) -> list[float]:
    """EWMA whose decay follows elapsed time rather than number of readings."""

    half_life = max(float(half_life_days), 0.01)
    decay = math.log(2.0) / half_life
    result: list[float] = []
    # This mirrors pandas' adjusted, time-aware EWM: every earlier reading is
    # weighted by its age at the current reading. A thousand daily points is
    # still only one million tiny arithmetic operations.
    for idx, current in enumerate(whens):
        weighted = total_weight = 0.0
        for earlier, value in zip(whens[: idx + 1], values[: idx + 1]):
            age_days = max(0.0, (current - earlier).total_seconds() / 86400.0)
            weight = math.exp(-decay * age_days)
            weighted += value * weight
            total_weight += weight
        result.append(weighted / total_weight)
    return result


def _daily_interpolation(
    whens: list[datetime],
    values: list[float],
) -> tuple[list[datetime], list[float]]:
    """Interpolate a dated series onto an inclusive daily grid."""

    if len(whens) == 1:
        return list(whens), list(values)
    days = max(0, (whens[-1].date() - whens[0].date()).days)
    grid_when = [whens[0] + timedelta(days=offset) for offset in range(days + 1)]
    grid_values: list[float] = []
    right = 1
    for current in grid_when:
        while right < len(whens) - 1 and whens[right] < current:
            right += 1
        left = max(0, right - 1)
        if current <= whens[0]:
            grid_values.append(values[0])
        elif current >= whens[-1]:
            grid_values.append(values[-1])
        else:
            width = (whens[right] - whens[left]).total_seconds()
            elapsed = (current - whens[left]).total_seconds()
            fraction = elapsed / width if width else 0.0
            grid_values.append(
                values[left] + (values[right] - values[left]) * fraction
            )
    return grid_when, grid_values


def _linear_slope_per_day(
    whens: list[datetime],
    values: list[float],
) -> float | None:
    """Least-squares slope for dated values, in units per day."""

    if len(whens) < 2:
        return None
    origin = whens[0]
    xs = [(when - origin).total_seconds() / 86400.0 for when in whens]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(values) / len(values)
    denominator = sum((x - mean_x) ** 2 for x in xs)
    if denominator <= 0:
        return None
    return sum(
        (x - mean_x) * (y - mean_y)
        for x, y in zip(xs, values)
    ) / denominator


def bodyweight_trend(
    whens: Iterable[datetime],
    weights_kg: Iterable[float],
    *,
    goal_kg: float | None = None,
    half_life_days: float = 7.0,
    rate_window_days: int = 14,
    fit_window_days: int = 28,
    projection_limit_days: int = 730,
) -> BodyweightTrend:
    """Build a time-aware bodyweight trend, rate series and goal projection.

    The inputs should already be collapsed to one reading per local day. They
    are sorted and invalid values are ignored defensively. A projection is only
    returned when at least three recent days point toward the configured goal;
    this avoids presenting a confident ETA from two noisy scale readings.
    """

    pairs: list[tuple[datetime, float]] = []
    for when, raw_weight in zip(whens, weights_kg):
        try:
            weight = float(raw_weight)
        except (TypeError, ValueError):
            continue
        if not isinstance(when, datetime) or not math.isfinite(weight):
            continue
        pairs.append((when, weight))
    pairs.sort(key=lambda pair: pair[0])
    if not pairs:
        raise ValueError("bodyweight trend needs at least one dated reading")

    logged_when = [pair[0] for pair in pairs]
    logged_kg = [pair[1] for pair in pairs]
    logged_trend = _time_aware_ewma(
        logged_when, logged_kg, half_life_days,
    )
    trend_when, trend_kg = _daily_interpolation(logged_when, logged_trend)

    residuals = [
        weight - smoothed
        for weight, smoothed in zip(logged_kg, logged_trend)
    ]
    noise_sd = statistics.stdev(residuals) if len(residuals) > 1 else 0.0

    rate_window = max(1, int(rate_window_days))
    rates: list[float | None] = [None] * len(trend_kg)
    for idx in range(rate_window, len(trend_kg)):
        rates[idx] = (
            (trend_kg[idx] - trend_kg[idx - rate_window])
            / rate_window
            * 7.0
        )

    projection_when: list[datetime] = []
    projection_kg: list[float] = []
    eta: date | None = None
    projection_rate: float | None = None
    finite_goal = (
        float(goal_kg)
        if goal_kg is not None and math.isfinite(float(goal_kg))
        else None
    )
    if finite_goal is not None and abs(finite_goal - trend_kg[-1]) >= 0.05:
        cutoff = logged_when[-1] - timedelta(days=max(1, int(fit_window_days)))
        fit = [
            (when, weight)
            for when, weight in zip(logged_when, logged_kg)
            if when >= cutoff
        ]
        if len(fit) >= 3:
            slope = _linear_slope_per_day(
                [pair[0] for pair in fit],
                [pair[1] for pair in fit],
            )
            needed = finite_goal - trend_kg[-1]
            if slope is not None and abs(slope) >= 0.001:
                projection_rate = slope * 7.0
            if (
                slope is not None
                and abs(slope) >= 0.001
                and needed * slope > 0
            ):
                days_out = needed / slope
                if 0 < days_out <= max(1, int(projection_limit_days)):
                    whole_days = math.ceil(days_out)
                    projection_when = [
                        logged_when[-1] + timedelta(days=min(day, days_out))
                        for day in range(whole_days + 1)
                    ]
                    projection_kg = [
                        trend_kg[-1] + slope * min(day, days_out)
                        for day in range(whole_days + 1)
                    ]
                    projection_kg[-1] = finite_goal
                    eta = projection_when[-1].date()

    return BodyweightTrend(
        logged_when=logged_when,
        logged_kg=logged_kg,
        logged_trend_kg=logged_trend,
        trend_when=trend_when,
        trend_kg=trend_kg,
        rate_kg_week=rates,
        noise_sd_kg=noise_sd,
        projection_when=projection_when,
        projection_kg=projection_kg,
        goal_kg=finite_goal,
        goal_eta=eta,
        projection_rate_kg_week=projection_rate,
    )

