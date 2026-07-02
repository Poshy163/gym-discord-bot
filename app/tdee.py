"""Maintenance-energy (TDEE) estimation from logged intake + bodyweight trend.

The idea: over a window where someone logged both their food and their
bodyweight, the gap between what they ate and how their weight moved reveals
their actual maintenance calories — no calculator formulas, just their own
data. A kilogram of body tissue is worth roughly 7 700 kcal, so:

    TDEE ≈ average daily intake − (weight change in kg/day × 7 700)

Losing weight while eating 2 200 cal/day at −0.4 kg/week means maintenance is
about 2 200 + (0.4 × 7700 / 7) ≈ 2 640 cal.

Pure and Discord-free for direct unit testing. All date bucketing happens in
the caller's display timezone before the values get here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

# Approximate energy density of a kilogram of body tissue. The classic
# "3 500 kcal per pound" rule of thumb; real tissue is a fat/lean mix so this
# is an estimate, which is why results are presented as ≈.
KCAL_PER_KG = 7700.0

# Guardrails: below/above these the inputs are almost certainly bad data
# (missed logs, a typo'd weigh-in) rather than a real human metabolism.
_MIN_PLAUSIBLE_TDEE = 1000.0
_MAX_PLAUSIBLE_TDEE = 6000.0


@dataclass
class TdeeEstimate:
    """Result of :func:`estimate_tdee` when there's enough data."""

    days_spanned: int        # days between first and last weigh-in used
    weighins: int            # number of weigh-ins in the window
    logged_days: int         # days with at least one calorie entry
    coverage: float          # logged_days / days_spanned (0..1)
    avg_intake_kcal: float   # mean daily intake on logged days
    kg_per_week: float       # bodyweight trend (negative = losing)
    tdee_kcal: float         # estimated maintenance
    start_kg: float          # trend value at the window start
    end_kg: float            # trend value at the window end


def _linreg_slope_per_day(points: list[tuple[datetime, float]]) -> float:
    """Least-squares slope of (timestamp, value) points in value-units/day.

    Regression beats an endpoint-to-endpoint slope here because day-to-day
    bodyweight is noisy (water, food in transit) — a single unlucky endpoint
    weigh-in would otherwise swing the whole estimate.
    """
    n = len(points)
    t0 = points[0][0]
    xs = [(ts - t0).total_seconds() / 86400.0 for ts, _ in points]
    ys = [kg for _, kg in points]
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:
        return 0.0
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denom


def estimate_tdee(
    weights: list[tuple[datetime, float]],
    day_totals: dict[date, float],
    *,
    min_days: int = 14,
    min_weighins: int = 4,
    min_coverage: float = 0.5,
) -> tuple[TdeeEstimate | None, str]:
    """Estimate maintenance calories from weigh-ins + daily intake totals.

    ``weights`` is (timestamp, kg) in any order; ``day_totals`` maps each
    local calendar date with at least one calorie entry to that day's total
    kcal. Returns ``(estimate, reason)`` — estimate None with a human reason
    when the data can't support a number:

    * fewer than ``min_weighins`` weigh-ins, or spanning under ``min_days``
      days (trend too noisy to trust);
    * intake logged on under ``min_coverage`` of the spanned days (unlogged
      days make the average meaningless);
    * a result outside plausible human range (bad data somewhere).
    """
    pts = sorted(weights, key=lambda r: r[0])
    if len(pts) < min_weighins:
        return None, (
            f"need at least {min_weighins} weigh-ins in the window "
            f"(have {len(pts)}) — log with `bodyweight 82.4` in chat"
        )
    first_ts, last_ts = pts[0][0], pts[-1][0]
    days_spanned = (last_ts - first_ts).days
    if days_spanned < min_days:
        return None, (
            f"weigh-ins need to span at least {min_days} days for a stable "
            f"trend (currently {days_spanned})"
        )

    # Intake average over the weigh-in window only — intake logged outside it
    # has no matching weight movement to compare against.
    first_day, last_day = first_ts.date(), last_ts.date()
    in_window = {
        d: kcal for d, kcal in day_totals.items() if first_day <= d <= last_day
    }
    # Spanned days is inclusive of both endpoints for coverage purposes.
    coverage = len(in_window) / float(days_spanned + 1)
    if not in_window or coverage < min_coverage:
        return None, (
            f"calories were only logged on {len(in_window)} of "
            f"{days_spanned + 1} days — need at least "
            f"{min_coverage:.0%} coverage for the average to mean anything"
        )

    avg_intake = sum(in_window.values()) / len(in_window)
    slope_per_day = _linreg_slope_per_day(pts)
    tdee = avg_intake - slope_per_day * KCAL_PER_KG
    if not (_MIN_PLAUSIBLE_TDEE <= tdee <= _MAX_PLAUSIBLE_TDEE):
        return None, (
            "the numbers don't add up to a plausible maintenance value — "
            "check for typo'd weigh-ins or missing food logs in the window"
        )

    return TdeeEstimate(
        days_spanned=days_spanned,
        weighins=len(pts),
        logged_days=len(in_window),
        coverage=coverage,
        avg_intake_kcal=avg_intake,
        kg_per_week=slope_per_day * 7.0,
        tdee_kcal=tdee,
        start_kg=pts[0][1],
        end_kg=pts[-1][1],
    ), ""
