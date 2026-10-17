"""What the forecaster predicts right now, for Live alerts.

For every bin: how full it is now, when the ML forecaster predicts it will be full, when its next
collection is (today's route, the evening round, or the next shift's plan), and how long it is expected
to overflow before then (with the chance it is already full when the truck arrives).
Sensor bins use the ML forecaster (run on the Telum / CPU engine chosen for this machine); bins without a
sensor have no live data for the model and use their learned pattern - each alert says which.

Also: bins rising faster than the forecaster predicted for the same hours (a "surge"), judged against the
model's prediction made at the start of that window - not against an average.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

import numpy as np

from . import anomaly, config, db, forecast, fmt, planner, progress, risk, state

HORIZON_H = 36
_CACHE: dict = {}


def next_visits(conn: sqlite3.Connection, now_ts: int) -> dict[str, dict]:
    """bin_id -> {"ts", "vehicle", "round"} for its next planned collection after now (approved plans first,
    then the next shift's draft), skipping stops already recorded."""
    out: dict[str, dict] = {}
    today = planner.today(now_ts).isoformat()
    nxt = planner.plan_day(conn, now_ts).isoformat()
    for day in dict.fromkeys([today, nxt]):
        for round_ in ("main", "extra"):
            p = planner.current(conn, day, round_=round_)
            if not p:
                continue
            for r in p["routes"]:
                states = progress.stop_states(conn, p, r) if p["status"] == "approved" else ["todo"] * len(r["stops"])
                for s, st_ in zip(r["stops"], states):
                    if s["kind"] != "bin" or st_ == "done" or s["arrive_ts"] < now_ts - 1800:
                        continue
                    cur = out.get(s["bin_id"])
                    if cur is None or s["arrive_ts"] < cur["ts"]:
                        out[s["bin_id"]] = {"ts": int(s["arrive_ts"]), "vehicle": r["vehicle_name"],
                                            "round": "evening round" if round_ == "extra" else
                                            ("today's route" if day == today else "next shift"),
                                            "approved": p["status"] == "approved"}
    return out


def predict(conn: sqlite3.Connection, now_ts: int | None = None) -> dict:
    """Per-bin prediction (cached for 5 minutes per data version)."""
    now_ts = now_ts or db.now()
    key = (db.data_version(conn), now_ts // 300)
    if _CACHE.get("key") == key:
        return _CACHE["value"]
    bins = db.bins(conn)
    try:
        bad = anomaly.faulty_sensors(conn, now_ts)
    except Exception:
        bad = set()
    prof = state.rate_profiles(conn, now_ts, bad_sensors=bad)
    st = state.estimate(conn, now_ts, prof, bad_sensors=bad)
    rates = {b["id"]: state.profile_rates(prof[b["id"]], now_ts, HORIZON_H) for b in bins}
    ml = forecast.predict_rates(conn, now_ts, HORIZON_H, [b for b in bins if b["id"] not in bad], st,
                                fallback=None, job="live alerts")
    ml_bins = set()
    if ml:
        for bid, r in ml["rates"].items():
            rates[bid] = np.asarray(r)[:HORIZON_H]
            ml_bins.add(bid)
    ids = [b["id"] for b in bins]
    curves = risk.build(ids, np.array([st[b].fill for b in ids]), np.array([st[b].sd for b in ids]),
                        np.array([rates[b] for b in ids]), now_ts, HORIZON_H * 60)
    visits = next_visits(conn, now_ts)
    out = {}
    for i, b in enumerate(bins):
        bid = b["id"]
        v = visits.get(bid)
        until = v["ts"] if v else now_ts + HORIZON_H * 3600
        full_at = None if st[bid].fill >= 100 else curves.full_eta(i)
        out[bid] = {"bin": b, "fill_now": float(st[bid].fill), "full_at": full_at, "full_now": st[bid].fill >= 100,
                    "next_visit": v, "overflow_min": float(curves.overflow_min_at(i, until)),
                    "p_full_at_visit": float(curves.p_full_at(i, until)),
                    "source": "ML forecaster" if bid in ml_bins else
                              ("learned pattern - sensor not trusted" if bid in bad else
                               "learned pattern - no sensor"),
                    "fill_at_visit": float(curves.fill_at(i, until))}
    value = {"bins": out, "engine": (ml or {}).get("source"), "ts": now_ts, "faulty": bad}
    _CACHE.update(key=key, value=value)
    return value


def model_surges(conn: sqlite3.Connection, now_ts: int, hours: int = 6, ratio: float = 1.6,
                 min_rise: float = 15.0) -> list[dict]:
    """Sensor bins that rose much faster over the last hours than the forecaster predicted for those hours
    (prediction made at the start of the window, from the data available then)."""
    key = ("surge", db.data_version(conn), now_ts // 300)
    if _CACHE.get("surge_key") == key:
        return _CACHE["surge"]
    since = now_ts - hours * 3600
    bins = [b for b in db.bins(conn) if b["has_sensor"]]
    ml = forecast.predict_rates(conn, since, hours + 1, bins, {}, fallback=None, job="surge check")
    if not ml:
        return []
    out = []
    for b in bins:
        r = ml["rates"].get(b["id"])
        if r is None:
            continue
        rs = conn.execute("SELECT ts, fill_pct, note FROM readings WHERE bin_id=? AND ts BETWEEN ? AND ?"
                          " AND fill_pct IS NOT NULL AND source IN ('sensor','api') ORDER BY ts",
                          (b["id"], since, now_ts)).fetchall()
        if len(rs) < 2:
            continue
        if conn.execute("SELECT 1 FROM events WHERE bin_id=? AND kind='collected' AND ts BETWEEN ? AND ?",
                        (b["id"], rs[0][0], rs[-1][0])).fetchone():
            continue
        h0, h1 = (rs[0][0] - since) / 3600, (rs[-1][0] - since) / 3600
        hrs = np.arange(len(r))
        w = np.clip(np.minimum(hrs + 1, h1) - np.maximum(hrs, h0), 0, 1)
        predicted = float(np.sum(np.asarray(r) * w))
        observed = float(rs[-1][1] - rs[0][1])
        if observed >= min_rise and observed > ratio * max(predicted, 1.0):
            out.append({"bin_id": b["id"], "landmark": b["landmark"], "observed": round(observed),
                        "predicted": round(predicted, 1), "ratio": round(observed / max(predicted, 1.0), 1),
                        "since_ts": int(rs[0][0]),
                        "test": any((x[2] or "").startswith("TEST EVENT") for x in rs)})
    out = sorted(out, key=lambda x: -x["ratio"])
    _CACHE.update(surge_key=key, surge=out)
    return out


def advice(conn: sqlite3.Connection, now_ts: int, visit: dict | None) -> str:
    """What can be done now, depending on the time of day."""
    s = db.get_settings(conn)
    start, end = planner.shift_window(s, planner.today(now_ts))
    hh, mm = map(int, s.get("extra_latest_start", "20:00").split(":"))
    d = datetime.fromtimestamp(now_ts, config.IST)
    latest_evening = int(d.replace(hour=hh, minute=mm, second=0).timestamp())
    if start <= now_ts < end:
        return "Re-plan from now (Today's plan) to bring it forward, or send the nearest crew."
    if end <= now_ts < latest_evening:
        return "Plan an evening round (Today's plan, below the plan) - or send the spare vehicle."
    if visit and visit["round"] == "next shift":
        return (f"It is on the next shift's plan at {fmt.hhmm(visit['ts'])} ({visit['vehicle']}); check it is near "
                "the start of the route before approving.")
    return "Make sure it is in the next shift's plan (Generate plan) - first stops go to the fullest bins."


def visit_text(v: dict | None) -> str:
    if not v:
        return "no collection planned yet"
    return f"next collection {fmt.when(v['ts'])} ({v['vehicle']}, {v['round']}{'' if v['approved'] else ', draft'})"
