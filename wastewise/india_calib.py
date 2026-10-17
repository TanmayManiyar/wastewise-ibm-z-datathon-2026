"""Measured Indian effects from real data (BBMP garbage complaints 2020-2025 + Open-Meteo rain).

Garbage complaints are a proxy for "bins and spots overflowing", not a fill-level measurement. Effects
are measured relative to a centred 28-day median of the same series, because the volume of complaints
changed a lot over the years (lockdowns in 2020-21, a much busier complaint system from 2024).

Measured: weekday profile, the days during and after each festival, and the day after rain (by amount).
Each effect has a 90 % bootstrap interval; effects whose interval includes "no change" are reported as
such and not used. The BBMP timestamps have no AM/PM (all hours are 00-11), so no hour-of-day effect is
taken from them.

Festival dates for Bengaluru 2020-2025 are the public holiday dates (Karnataka) - approximate for
multi-day festivals.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import numpy as np

from . import india_data

# name -> main day per year (Karnataka public holiday list)
FESTIVAL_DAYS = {
    "Ganesh Chaturthi": ["2020-08-22", "2021-09-10", "2022-08-31", "2023-09-18", "2024-09-07", "2025-08-27"],
    "Dasara / Vijayadashami": ["2020-10-26", "2021-10-15", "2022-10-05", "2023-10-24", "2024-10-12"],
    "Deepavali": ["2020-11-14", "2021-11-04", "2022-10-24", "2023-11-12", "2024-10-31"],
    "Ugadi": ["2020-03-25", "2021-04-13", "2022-04-02", "2023-03-22", "2024-04-09", "2025-03-30"],
    "Bakrid": ["2020-08-01", "2021-07-21", "2022-07-10", "2023-06-29", "2024-06-17", "2025-06-07"],
    "Makara Sankranti": ["2021-01-14", "2022-01-14", "2023-01-15", "2024-01-15", "2025-01-14"],
    "Christmas / New Year": ["2020-12-25", "2021-12-25", "2022-12-25", "2023-12-25", "2024-12-25"],
}
WINDOWS = {"day before": (-1, -1), "festival day": (0, 0), "1-3 days after": (1, 3)}
RAIN_BINS = [("dry (<0.5 mm)", 0.0, 0.5), ("light (0.5-5 mm)", 0.5, 5.0), ("moderate (5-20 mm)", 5.0, 20.0),
             ("heavy (>20 mm)", 20.0, 1e9)]
CALIB_FILE = "calibration.json"


def daily_counts(subcats: set[str] | None = None) -> dict[date, int]:
    import csv
    out: dict[date, int] = {}
    with (india_data.data_dir() / "bbmp_garbage_complaints.csv").open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if subcats and r["Sub Category"] not in subcats:
                continue
            d = date.fromisoformat(r["Grievance Date"][:10])
            out[d] = out.get(d, 0) + 1
    if not out:
        return out
    d, last = min(out), max(out)
    while d <= last:
        out.setdefault(d, 0)
        d += timedelta(days=1)
    return dict(sorted(out.items()))


def relative(series: dict[date, int], window: int = 28) -> dict[date, float]:
    """Each day's count divided by the centred median of the surrounding `window` days."""
    days = list(series)
    vals = np.array([series[d] for d in days], dtype=float)
    h = window // 2
    out = {}
    for i, d in enumerate(days):
        lo, hi = max(0, i - h), min(len(vals), i + h + 1)
        med = float(np.median(vals[lo:hi]))
        if med >= 5:
            out[d] = vals[i] / med
    return out


def _boot(x: np.ndarray, rng: np.random.Generator, n: int = 2000) -> tuple[float, float, float]:
    if len(x) == 0:
        return float("nan"), float("nan"), float("nan")
    means = rng.choice(x, size=(n, len(x)), replace=True).mean(axis=1)
    return float(x.mean()), float(np.percentile(means, 5)), float(np.percentile(means, 95))


def _effect(name: str, x: np.ndarray, rng) -> dict:
    m, lo, hi = _boot(np.asarray(x, dtype=float), rng)
    return {"effect": name, "factor": round(m, 3), "low90": round(lo, 3), "high90": round(hi, 3), "days": int(len(x)),
            "clear": bool(lo > 1.0 or hi < 1.0)}


def calibrate(subcats: set[str] | None = None, seed: int = 7) -> dict:
    """All measured effects; also written to data/india/calibration.json."""
    rng = np.random.default_rng(seed)
    rel = relative(daily_counts(subcats))
    fest_days = {date.fromisoformat(s) for v in FESTIVAL_DAYS.values() for s in v}
    near_fest = {d + timedelta(days=k) for d in fest_days for k in range(-2, 5)}
    plain = {d: v for d, v in rel.items() if d not in near_fest}

    weekday = [_effect(n, [v for d, v in plain.items() if d.weekday() == k], rng)
               for k, n in enumerate(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])]

    festivals = []
    for name, days in FESTIVAL_DAYS.items():
        for wname, (a, b) in WINDOWS.items():
            vals = [rel[d0 + timedelta(days=k)] for s in days for d0 in [date.fromisoformat(s)]
                    for k in range(a, b + 1) if d0 + timedelta(days=k) in rel]
            festivals.append(dict(_effect(wname, vals, rng), festival=name))
    all_after = [rel[date.fromisoformat(s) + timedelta(days=k)] for v in FESTIVAL_DAYS.values() for s in v
                 for k in (1, 2, 3) if date.fromisoformat(s) + timedelta(days=k) in rel]

    rain = india_data.rain_daily("bengaluru")
    rain_eff = []
    for label, lo, hi in RAIN_BINS:
        vals = [v for d, v in plain.items() if lo <= rain.get(d - timedelta(days=1), -1) < hi]
        rain_eff.append(_effect(f"day after {label} rain", vals, rng))
    # monsoon months vs the rest (Bengaluru SW monsoon Jun-Sep), on raw counts relative to a 365-day median
    out = {"source": india_data.provenance().get("bbmp", {}).get("dataset"),
           "complaints_used": int(sum(daily_counts(subcats).values())), "subcategories": sorted(subcats or []),
           "baseline": "centred 28-day median of daily garbage complaints",
           "weekday": weekday, "festivals": festivals,
           "festival_any_1_3_days_after": _effect("1-3 days after any festival", all_after, rng),
           "rain": rain_eff,
           "note": "BBMP timestamps lack AM/PM, so hour-of-day effects are not measured."}
    (india_data.data_dir() / CALIB_FILE).write_text(json.dumps(out, indent=2))
    return out


def load() -> dict | None:
    p = india_data.data_dir() / CALIB_FILE
    return json.loads(p.read_text()) if p.exists() else None


def factor(calib: dict | None, kind: str, key: str, default: float = 1.0) -> float:
    """A measured factor if it is clearly different from 1 (90 % interval), else `default`."""
    if not calib:
        return default
    rows = {"weekday": calib["weekday"], "rain": calib["rain"]}.get(kind, [])
    for r in rows:
        if r["effect"] == key:
            return r["factor"] if r["clear"] else default
    return default
