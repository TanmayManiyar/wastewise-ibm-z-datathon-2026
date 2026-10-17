"""Indian calibration: measured effects, post-festival surge, ward-risk metrics."""
from datetime import date, timedelta

import numpy as np
import pytest

from wastewise import calendar_in, india_calib, india_data, india_risk


def test_relative_series_removes_the_trend():
    d0 = date(2024, 1, 1)
    series = {d0 + timedelta(days=i): 10 + i for i in range(120)}          # steadily rising volume
    series[d0 + timedelta(days=60)] *= 2                                     # one busy day
    rel = india_calib.relative(series)
    assert abs(rel[d0 + timedelta(days=40)] - 1.0) < 0.05
    assert rel[d0 + timedelta(days=60)] > 1.8


def test_effect_needs_a_clear_interval():
    rng = np.random.default_rng(0)
    clear = india_calib._effect("x", 1.3 + rng.normal(0, 0.05, 60), rng)
    unclear = india_calib._effect("y", 1.0 + rng.normal(0, 0.2, 60), rng)
    assert clear["clear"] and clear["low90"] > 1.0
    assert not unclear["clear"]
    cal = {"weekday": [], "rain": [dict(clear, effect="heavy"), dict(unclear, effect="light")]}
    assert india_calib.factor(cal, "rain", "heavy") == clear["factor"]
    assert india_calib.factor(cal, "rain", "light") == 1.0          # not clear: not used


def test_post_festival_surge(monkeypatch):
    monkeypatch.setattr(calendar_in, "_AFTER", {"*": 1.15, "Dasara / Vijayadashami": 1.5})
    end = date(2026, 10, 20)                                          # Dussehra (one-day festival)
    assert calendar_in.days_after_festival(end + timedelta(days=1)) == (1, "Dussehra")
    assert calendar_in.after_festival_multiplier(end + timedelta(days=2)) == 1.5
    assert calendar_in.after_festival_multiplier(date(2026, 3, 6)) == 1.15     # 2 days after Holi -> average
    assert calendar_in.after_festival_multiplier(date(2026, 7, 1)) == 1.0      # no festival nearby
    monkeypatch.setattr(calendar_in, "_AFTER", {})
    assert calendar_in.after_festival_multiplier(end + timedelta(days=2)) == 1.0  # not calibrated: no effect


def test_ward_risk_metrics():
    y = np.array([[5, 0, 1, 0], [0, 3, 0, 2]], dtype=float)
    perfect = india_risk._metrics(y, y + 1e-9, k=2)
    flat = india_risk._metrics(y, np.full_like(y, y.mean()), k=2)
    assert perfect["mae"] < 1e-6 and perfect["top2_hit_rate"] == 1.0
    assert flat["poisson_deviance"] > perfect["poisson_deviance"]


@pytest.mark.skipif(not (india_data.data_dir() / "bbmp_garbage_complaints.csv").exists(),
                    reason="BBMP data not downloaded (python -m wastewise.cli india-data)")
def test_bengaluru_risk_model_beats_the_baselines_on_later_years():
    r = india_risk.evaluate()
    assert r["test"].startswith("2024-01-01")
    m, b = r["results"]["model"], r["results"]["baseline_28day_mean_x_weekday"]
    assert m["poisson_deviance"] < b["poisson_deviance"] and m["mae"] < b["mae"]
