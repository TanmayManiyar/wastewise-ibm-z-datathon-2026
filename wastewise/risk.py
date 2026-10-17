"""Overflow risk as a function of when a truck can visit.

For each bin the fill at time t is modelled as Normal(mu(t), sd(t)) where
mu(t) = current estimate + expected filling until t and sd(t) widens with
the amount of filling still to come. From this:
  * p_full(t)            probability the bin is full (>= 100 %) at time t,
  * overflow_min(T)      expected minutes the bin spends overflowing between
                         the plan start and a visit at time T,
  * full_eta             time the expected fill reaches 100 %.
Curves are sampled every few minutes over the shift so the optimiser can look
up the cost of any arrival time in O(1).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

STEP_S = 300


def _norm_sf(z: np.ndarray) -> np.ndarray:
    """P(Z > z) for a standard normal, vectorised via erfc."""
    return 0.5 * np.vectorize(math.erfc)(z / math.sqrt(2))


@dataclass
class RiskCurves:
    bin_ids: list[str]
    start_ts: int
    step_s: int
    mu: np.ndarray            # (n, K) expected fill %
    sd: np.ndarray            # (n, K)
    p_full: np.ndarray        # (n, K)
    cum_overflow_min: np.ndarray  # (n, K) expected overflow minutes since start
    overflowing_since: list[int | None]  # estimated time overflow began (if already full at start)

    def _k(self, ts: float) -> float:
        return min(max((ts - self.start_ts) / self.step_s, 0.0), self.mu.shape[1] - 1.0)

    def _interp(self, arr: np.ndarray, i: int, ts: float) -> float:
        k = self._k(ts)
        k0 = int(k)
        k1 = min(k0 + 1, arr.shape[1] - 1)
        f = k - k0
        return float(arr[i, k0] * (1 - f) + arr[i, k1] * f)

    def fill_at(self, i: int, ts: float) -> float:
        return self._interp(self.mu, i, ts)

    def p_full_at(self, i: int, ts: float) -> float:
        return self._interp(self.p_full, i, ts)

    def overflow_min_at(self, i: int, ts: float) -> float:
        return self._interp(self.cum_overflow_min, i, ts)

    def full_eta(self, i: int) -> int | None:
        k = np.nonzero(self.mu[i] >= 100.0)[0]
        return None if len(k) == 0 else int(self.start_ts + k[0] * self.step_s)

    def status_at(self, i: int, ts: float) -> str:
        """Colour class for the map: red = full at start, orange = full by arrival,
        amber = over half, green = fine."""
        from .fmt import status_plan
        return status_plan(float(self.mu[i, 0]), self.fill_at(i, ts))


def build(bin_ids: list[str], mu0: np.ndarray, sd0: np.ndarray, rates: np.ndarray, start_ts: int,
          horizon_min: int, step_s: int = STEP_S, rate_cv: float = 0.25) -> RiskCurves:
    """rates: (n, H) expected filling in %/hour for each hour from start_ts."""
    n = len(bin_ids)
    K = horizon_min * 60 // step_s + 1
    hours = (np.arange(K) * step_s) / 3600.0
    H = rates.shape[1]
    # Cumulative expected growth at each grid time (piecewise-constant hourly rates).
    cum_h = np.concatenate([np.zeros((n, 1)), np.cumsum(rates, axis=1)], axis=1)  # (n, H+1)
    # Beyond the last forecast hour, keep filling at the last hourly rate.
    whole = np.minimum(np.floor(hours).astype(int), H)
    growth = cum_h[:, whole] + rates[:, np.minimum(whole, H - 1)] * (hours - whole)
    mu = mu0[:, None] + growth
    sd = np.sqrt(sd0[:, None] ** 2 + (rate_cv * growth) ** 2 + 0.25 * hours[None, :])
    p = _norm_sf((100.0 - mu) / np.maximum(sd, 1e-6))
    dt_min = step_s / 60.0
    cum = np.concatenate([np.zeros((n, 1)), np.cumsum((p[:, 1:] + p[:, :-1]) / 2 * dt_min, axis=1)], axis=1)
    since: list[int | None] = []
    for i in range(n):
        if mu0[i] >= 100:
            r = max(float(rates[i, 0]), 0.5)
            since.append(int(start_ts - (mu0[i] - 100) / r * 3600))
        else:
            since.append(None)
    return RiskCurves(bin_ids, start_ts, step_s, mu, sd, p, cum, since)
