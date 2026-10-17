"""Evening round: which bins fill up again after the morning round and would overflow before
tomorrow's round, and the best time to send a second pickup.

For every bin the fill is projected forward with the forecaster's hourly rates: from its planned
morning visit (emptied), or from its current estimate if it is not on the morning route, until its
next regular visit tomorrow. Bins expected to overflow for at least `min_overflow_min` before then
are candidates. The suggested start time is chosen by trying every half hour between the end of the
morning shift and the latest allowed start, and keeping the one with the least total overflow:
collecting too early lets bins fill up again overnight, collecting too late lets them overflow first.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta

import numpy as np

from . import config, db, planner

STEP_S = 600          # projection resolution (10 min)
VISIT_SPREAD_S = 3600  # a round reaches its bins on average about an hour after it starts


def _rate_fn(rates: np.ndarray, start_ts: int):
    """Hourly %/h array from start_ts -> function giving the rate at any time (repeats the last day)."""
    n = len(rates)

    def at(ts: float) -> float:
        k = int((ts - start_ts) // 3600)
        if k < 0:
            k = 0
        if k >= n:
            k = n - 24 + (k - n) % 24 if n >= 24 else n - 1
        return float(rates[k])
    return at


def _project(f0: float, t0: int, t1: int, rate) -> tuple[np.ndarray, np.ndarray]:
    ts = np.arange(t0, max(t1, t0 + STEP_S) + 1, STEP_S)
    f = np.empty(len(ts))
    v = f0
    for i, t in enumerate(ts):
        f[i] = v
        v += rate(t) * STEP_S / 3600
    return ts, f


def _overflow_min(ts: np.ndarray, f: np.ndarray, until: int) -> float:
    m = (ts < until) & (f >= 100)
    return float(m.sum() * STEP_S / 60)


def assess(conn: sqlite3.Connection, plan: dict, min_overflow_min: float = 60.0) -> dict:
    """plan = the day's regular (morning) plan."""
    s = db.get_settings(conn)
    bins = {b["id"]: b for b in db.bins(conn)}
    day = date.fromisoformat(plan["plan_date"])
    _, morning_end = planner.shift_window(s, day)
    next_start, _ = planner.shift_window(s, day + timedelta(days=1))
    start = int(plan["start_ts"])
    st, rates, _, src = planner._rates(conn, start, 48, list(bins.values()), set(plan.get("bad_sensors", [])))
    visits = {stp["bin_id"]: stp["arrive_ts"] for r in plan["routes"] for stp in r["stops"] if stp["kind"] == "bin"}
    hh, mm = map(int, s.get("extra_latest_start", "20:00").split(":"))
    latest = int(datetime(day.year, day.month, day.day, hh, mm, tzinfo=config.IST).timestamp())
    earliest = morning_end + 1800

    cands = []
    for bid, b in bins.items():
        rate = _rate_fn(np.asarray(rates[bid]), start)
        if bid in visits:
            t0, f0 = int(visits[bid]), 2.0
            t_next = t0 + 86400
        else:
            t0, f0 = start, float(st[bid].fill)
            t_next = next_start + 3600
        ts, f = _project(f0, t0, t_next, rate)
        over = _overflow_min(ts, f, t_next)
        if over < min_overflow_min:
            continue
        full_idx = np.nonzero(f >= 100)[0]
        t_full = int(ts[full_idx[0]]) if len(full_idx) else None
        if t_full is not None and t_full > latest + 6 * 3600 and bid in visits:
            continue        # only overflows near tomorrow's round: not worth an evening trip
        cands.append({"bin_id": bid, "landmark": b["landmark"], "street": b["street"], "full_at": t_full,
                      "overflow_min": round(over), "on_morning_route": bid in visits, "t0": t0, "f0": f0,
                      "t_next": t_next, "_rate": rate})

    best = None
    if cands:
        for start_try in range(int(earliest // 1800 * 1800 + 1800), latest + 1, 1800):
            visit = start_try + VISIT_SPREAD_S
            total = 0.0
            for cnd in cands:
                ts, f = _project(cnd["f0"], cnd["t0"], cnd["t_next"], cnd["_rate"])
                before = _overflow_min(ts, f, visit)                      # overflowing until the evening visit
                ts2, f2 = _project(2.0, visit, cnd["t_next"], cnd["_rate"])  # refills after the evening visit
                total += before + _overflow_min(ts2, f2, cnd["t_next"])
            if best is None or total < best[1]:
                best = (start_try, total)
    total_if_none = float(sum(c["overflow_min"] for c in cands))
    hours = float(s.get("extra_round_hours", 3.0))
    for c in cands:
        for k in ("_rate", "t0", "f0"):
            c.pop(k, None)
    cands.sort(key=lambda c: (c["full_at"] or 0))
    return {"plan_id": plan.get("id"), "plan_date": plan["plan_date"], "forecast_source": src,
            "candidates": cands, "overflow_min_without": round(total_if_none),
            "suggested_start_ts": best[0] if best else None,
            "suggested_end_ts": int(best[0] + hours * 3600) if best else None,
            "overflow_min_with": round(best[1]) if best else 0,
            "assume_collected": {b: int(t) for b, t in visits.items()},
            "label": "computed from SYNTHETIC demonstration data" if db.get_meta(conn, "synthetic_info") else None}


def plan_round(conn: sqlite3.Connection, user: str, base: dict, start_ts: int, end_ts: int, bins: list[str],
               now_ts: int | None = None, assessment: dict | None = None, progress=None) -> dict:
    """Build the evening (extra) plan for the given slot and bins."""
    if not bins:
        raise planner.PlanError("Choose at least one bin for the evening round")
    assume = (assessment or {}).get("assume_collected") or {
        stp["bin_id"]: stp["arrive_ts"] for r in base["routes"] for stp in r["stops"] if stp["kind"] == "bin"}
    p = planner.generate(conn, user, kind="extra", now_ts=now_ts,
                         extra={"plan_date": base["plan_date"], "start_ts": int(start_ts), "end_ts": int(end_ts),
                                "bins": list(bins), "assume_collected": assume}, progress=progress)
    p["evening"] = {"slot": [int(start_ts), int(end_ts)], "requested_bins": list(bins),
                    "suggested": bool(assessment and assessment.get("suggested_start_ts") == start_ts)}
    return p


_CACHE: dict = {}


def assess_cached(conn: sqlite3.Connection, plan: dict) -> dict:
    """assess() recomputed only when the plan or the data changes (live alerts refresh every few seconds)."""
    key = (plan.get("id"), db.data_version(conn))
    if _CACHE.get("key") != key:
        _CACHE.clear()
        _CACHE["key"] = key
        _CACHE["value"] = assess(conn, plan)
    return _CACHE["value"]
