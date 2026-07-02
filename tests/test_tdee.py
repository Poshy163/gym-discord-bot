"""Tests for maintenance-energy estimation (app/tdee.py)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.tdee import KCAL_PER_KG, estimate_tdee


def _weights_linear(
    start: datetime, days: int, start_kg: float, kg_per_day: float,
    every: int = 2,
) -> list[tuple[datetime, float]]:
    """Evenly spaced weigh-ins along a perfect linear trend."""
    return [
        (start + timedelta(days=d), start_kg + kg_per_day * d)
        for d in range(0, days + 1, every)
    ]


def _totals(start: datetime, days: int, kcal: float) -> dict[date, float]:
    return {
        (start + timedelta(days=d)).date(): kcal for d in range(days + 1)
    }


_T0 = datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc)


def test_estimate_tdee_deficit():
    # Losing 0.05 kg/day on 2200 kcal → maintenance ≈ 2200 + 0.05*7700 = 2585.
    weights = _weights_linear(_T0, 28, 84.0, -0.05)
    est, reason = estimate_tdee(weights, _totals(_T0, 28, 2200.0))
    assert reason == ""
    assert est is not None
    assert est.tdee_kcal == pytest.approx(2200 + 0.05 * KCAL_PER_KG, rel=1e-6)
    assert est.kg_per_week == pytest.approx(-0.35, rel=1e-6)
    assert est.days_spanned == 28
    assert est.coverage == pytest.approx(1.0)


def test_estimate_tdee_surplus():
    # Gaining 0.03 kg/day on 3000 kcal → maintenance ≈ 3000 - 231 = 2769.
    weights = _weights_linear(_T0, 21, 78.0, 0.03)
    est, reason = estimate_tdee(weights, _totals(_T0, 21, 3000.0))
    assert est is not None and reason == ""
    assert est.tdee_kcal == pytest.approx(3000 - 0.03 * KCAL_PER_KG, rel=1e-6)
    assert est.kg_per_week > 0


def test_estimate_tdee_needs_enough_weighins():
    weights = _weights_linear(_T0, 28, 84.0, -0.05, every=14)  # only 3 points
    est, reason = estimate_tdee(weights, _totals(_T0, 28, 2200.0))
    assert est is None
    assert "weigh-ins" in reason


def test_estimate_tdee_needs_span():
    weights = _weights_linear(_T0, 10, 84.0, -0.05, every=2)  # 10-day span
    est, reason = estimate_tdee(weights, _totals(_T0, 10, 2200.0))
    assert est is None
    assert "span" in reason


def test_estimate_tdee_needs_coverage():
    weights = _weights_linear(_T0, 28, 84.0, -0.05)
    # Only 5 logged days out of 29.
    sparse = {
        (_T0 + timedelta(days=d)).date(): 2200.0 for d in range(0, 10, 2)
    }
    est, reason = estimate_tdee(weights, sparse)
    assert est is None
    assert "logged" in reason


def test_estimate_tdee_intake_outside_window_ignored():
    weights = _weights_linear(_T0, 28, 84.0, -0.05)
    totals = _totals(_T0, 28, 2200.0)
    # A wild entry well before the first weigh-in must not skew the average.
    totals[(_T0 - timedelta(days=30)).date()] = 9000.0
    est, reason = estimate_tdee(weights, totals)
    assert est is not None and reason == ""
    assert est.avg_intake_kcal == pytest.approx(2200.0)


def test_estimate_tdee_rejects_implausible():
    # A 5 kg/week "loss" (typo'd weigh-ins) implies a silly TDEE — refuse.
    weights = _weights_linear(_T0, 28, 90.0, -0.7)
    est, reason = estimate_tdee(weights, _totals(_T0, 28, 2000.0))
    assert est is None
    assert "plausible" in reason


def test_estimate_tdee_flat_weight_is_maintenance():
    weights = _weights_linear(_T0, 28, 84.0, 0.0)
    est, reason = estimate_tdee(weights, _totals(_T0, 28, 2500.0))
    assert est is not None and reason == ""
    assert est.tdee_kcal == pytest.approx(2500.0)
    assert est.kg_per_week == pytest.approx(0.0)
