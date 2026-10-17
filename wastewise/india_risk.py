"""Real-data test in India: predicting tomorrow's garbage complaints per ward in Bengaluru.

Target: number of garbage complaints (dumps, vehicle not arrived, dustbins not cleaned, dumping in
vacant sites) per BBMP ward per day - a real, public Indian signal of overflowing waste.
Features known the evening before: the ward's recent complaints (yesterday, last 7 and 28 days), the
weekday, festival timing (day of / 1-3 days after), and real rain (Open-Meteo) in the last 1 and 3 days.
Model: scikit-learn HistGradientBoosting with a Poisson loss.
Chronological test: trained on 2020-2023, tested on 2024-2025 (never seen in training), against
  * the ward's 28-day average, and
  * the ward's 28-day average times the weekday profile.
Also measured: of the 20 wards predicted riskiest each day, how many are really among the 20 with the
most complaints ("top-20 hit rate"), which is how a control room would use it.
"""
from __future__ import annotations

import csv
import json
import time
from datetime import date, timedelta

import numpy as np

from . import india_calib, india_data

OVERFLOW = {"Garbage dump", "Garbage vehicle not arrived", "Public dustbins not cleaned",
            "Garbage dumping in vacant sites"}
FEATURES = ["lag1", "mean7", "mean28", "share28", "city_mean7", "dow", "fest_day", "days_after_fest",
            "rain_1d", "rain_3d", "month"]
TEST_FROM = date(2024, 1, 1)
RESULT_FILE = "bengaluru_risk_test.json"


def panel() -> tuple[list[date], list[str], np.ndarray]:
    """counts[day, ward]"""
    rows = []
    with (india_data.data_dir() / "bbmp_garbage_complaints.csv").open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["Sub Category"] in OVERFLOW and r["Ward Name"]:
                rows.append((date.fromisoformat(r["Grievance Date"][:10]), r["Ward Name"].strip()))
    d0, d1 = min(d for d, _ in rows), max(d for d, _ in rows)
    days = [d0 + timedelta(days=i) for i in range((d1 - d0).days + 1)]
    wards = sorted({w for _, w in rows})
    di, wi = {d: i for i, d in enumerate(days)}, {w: i for i, w in enumerate(wards)}
    c = np.zeros((len(days), len(wards)), dtype=np.int32)
    for d, w in rows:
        c[di[d], wi[w]] += 1
    return days, wards, c


def _fest_feats(days: list[date]) -> tuple[np.ndarray, np.ndarray]:
    fest = {date.fromisoformat(s) for v in india_calib.FESTIVAL_DAYS.values() for s in v}
    on = np.array([int(d in fest) for d in days])
    after = np.full(len(days), 8)
    for i, d in enumerate(days):
        for k in range(1, 8):
            if d - timedelta(days=k) in fest:
                after[i] = k
                break
    return on, after


def features(days, wards, c):
    """X[t, w] uses information up to day t-1 to predict c[t, w]."""
    T, W = c.shape
    cs = np.vstack([np.zeros((1, W)), np.cumsum(c, axis=0)])
    def mean_back(t, n):           # mean over days t-n .. t-1
        lo = np.maximum(t - n, 0)
        return (cs[t] - cs[lo]) / np.maximum(t - lo, 1)[:, None]
    t = np.arange(T)
    lag1 = np.vstack([np.zeros((1, W)), c[:-1]])
    m7, m28 = mean_back(t, 7), mean_back(t, 28)
    city7 = m7.sum(axis=1, keepdims=True)
    share = m28 / np.maximum(m28.sum(axis=1, keepdims=True), 1e-9)
    on, after = _fest_feats(days)
    rain = india_data.rain_daily("bengaluru")
    r1 = np.array([rain.get(d - timedelta(days=1), np.nan) for d in days])
    r3 = np.array([sum(rain.get(d - timedelta(days=k), 0.0) for k in (1, 2, 3)) for d in days])
    dow = np.array([d.weekday() for d in days])
    month = np.array([d.month for d in days])
    rep = lambda a: np.repeat(a[:, None], W, axis=1)  # noqa: E731
    X = np.stack([lag1, m7, m28, share, np.repeat(city7, W, axis=1), rep(dow), rep(on), rep(after), rep(r1), rep(r3),
                  rep(month)], axis=-1)
    return X


def _metrics(y, p, k=20):
    mae = float(np.mean(np.abs(y - p)))
    p_ = np.maximum(p, 1e-6)
    dev = float(2 * np.mean(np.where(y > 0, y * np.log(np.maximum(y, 1e-12) / p_), 0) - (y - p_)))
    hits = []
    for yt, pt in zip(y, p):
        if yt.sum() < k:
            continue
        top_true = set(np.argsort(-yt, kind="stable")[:k])
        top_pred = set(np.argsort(-pt, kind="stable")[:k])
        hits.append(len(top_true & top_pred) / k)
    return {"mae": round(mae, 4), "poisson_deviance": round(dev, 4), f"top{k}_hit_rate": round(float(np.mean(hits)), 3),
            "days_scored_top": len(hits)}


def evaluate(seed: int = 0) -> dict:
    from sklearn.ensemble import HistGradientBoostingRegressor
    t0 = time.time()
    days, wards, c = panel()
    X = features(days, wards, c)
    start = 35                                         # need 28 days of history
    test0 = days.index(TEST_FROM)
    tr = slice(start, test0)
    te = slice(test0, len(days))
    Xtr, ytr = X[tr].reshape(-1, X.shape[-1]), c[tr].reshape(-1)
    model = HistGradientBoostingRegressor(loss="poisson", max_iter=300, learning_rate=0.06, max_leaf_nodes=31,
                                          min_samples_leaf=200, random_state=seed)
    model.fit(Xtr, ytr)
    yte = c[te].astype(float)
    pred = model.predict(X[te].reshape(-1, X.shape[-1])).reshape(yte.shape)
    m28 = X[te][..., FEATURES.index("mean28")]
    # weekday profile learned on the training years only
    tr_days = days[tr]
    tot = c[tr].sum(axis=1)
    dowm = np.array([np.mean([tot[i] for i, d in enumerate(tr_days) if d.weekday() == k]) for k in range(7)])
    dowf = dowm / dowm.mean()
    base2 = m28 * dowf[[d.weekday() for d in days[te]]][:, None]
    res = {"model": _metrics(yte, pred), "baseline_28day_mean": _metrics(yte, m28),
           "baseline_28day_mean_x_weekday": _metrics(yte, base2)}
    best_base = min(res["baseline_28day_mean"]["poisson_deviance"], res["baseline_28day_mean_x_weekday"]["poisson_deviance"])
    out = {"what": "Tomorrow's overflow-type garbage complaints per BBMP ward (real data, Bengaluru)",
           "wards": len(wards), "train": f"{days[start].isoformat()} to {days[test0 - 1].isoformat()}",
           "test": f"{days[test0].isoformat()} to {days[-1].isoformat()}", "test_ward_days": int(yte.size),
           "complaints_in_test": int(yte.sum()), "results": res,
           "deviance_improvement_vs_best_baseline_pct": round(100 * (1 - res["model"]["poisson_deviance"] / best_base), 1),
           "features": FEATURES, "seconds": round(time.time() - t0, 1),
           "source": india_data.provenance().get("bbmp", {}).get("dataset")}
    (india_data.data_dir() / RESULT_FILE).write_text(json.dumps(out, indent=2))
    return out


def load_result() -> dict | None:
    p = india_data.data_dir() / RESULT_FILE
    return json.loads(p.read_text()) if p.exists() else None
