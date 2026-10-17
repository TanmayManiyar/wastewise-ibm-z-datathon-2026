"""Sensor-fault and surge detection.

Faults: plain rules a supervisor can understand, combined with an
IsolationForest (scikit-learn) over per-reading features:
  * stuck      - the same value for 12+ hours while the bin is not full
  * spike      - a jump of 35+ points that reverts at the next reading
  * jump       - an impossible rise (45+ points within ~1.5 h)
  * no drop    - still reading over 25 % shortly after a confirmed collection
  * offline    - no reading for 6+ hours
A sensor flagged in the last 24 h is treated as faulty: the planner then
uses manual readings and the learned fill rate for that bin instead.

Surges: the bin rose much faster over the last 6 h than its normal pattern
predicts (festival, market day, event) - an early warning for the control room.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

import numpy as np

from . import config, db, state

RULES = ("stuck", "spike", "jump", "no_drop")


def rule_flags(ts: np.ndarray, v: np.ndarray, collections: np.ndarray | None = None) -> dict[str, np.ndarray]:
    n = len(v)
    out = {k: np.zeros(n, dtype=bool) for k in RULES}
    if n < 3:
        return out
    collections = np.sort(collections) if collections is not None and len(collections) else np.array([], dtype=np.int64)

    def collected_between(a, b):
        if not len(collections):
            return np.zeros(len(a), dtype=bool)
        i = np.searchsorted(collections, a, side="right")
        return (i < len(collections)) & (collections[np.minimum(i, len(collections) - 1)] <= b)

    d_prev = np.r_[0.0, np.diff(v)]
    coll_prev = np.r_[False, collected_between(ts[:-1], ts[1:])]
    # spike: big step that reverts at the next reading (and is not explained by a collection)
    rev = np.r_[False, np.abs(v[2:] - v[:-2]) <= 10, False]
    out["spike"] = (np.abs(d_prev) >= 35) & rev & ~coll_prev
    # impossible rise
    dt_h = np.r_[1.0, np.diff(ts) / 3600]
    out["jump"] = (d_prev >= 45) & (dt_h <= 1.6) & ~out["spike"]
    # stuck: runs of (almost) identical values below "full"
    same = np.r_[False, np.abs(np.diff(v)) < 0.05]
    run = np.zeros(n, dtype=int)
    for i in range(1, n):
        run[i] = run[i - 1] + 1 if same[i] else 0
    stuck = (run >= 11) & (v < 97)
    # mark the whole run once it is long enough
    for i in np.nonzero(stuck)[0]:
        out["stuck"][i - run[i]:i + 1] = True
    # no drop after a confirmed collection
    for c in collections:
        j = np.nonzero((ts > c + 1800) & (ts <= c + 5400))[0]
        if len(j) and np.median(v[j]) > 25:   # an emptied bin should read near zero
            k = np.nonzero((ts > c) & (ts <= c + 12 * 3600))[0]
            out["no_drop"][k] = True
    return out


def features(ts: np.ndarray, v: np.ndarray) -> np.ndarray:
    n = len(v)
    d_prev = np.r_[0.0, np.diff(v)]
    d_next = np.r_[np.diff(v), 0.0]
    pad = np.pad(v, 3, mode="edge")
    win = np.lib.stride_tricks.sliding_window_view(pad, 7)
    roll_std = win.std(axis=1)[:n]
    pad24 = np.pad(v, 12, mode="edge")
    med24 = np.median(np.lib.stride_tricks.sliding_window_view(pad24, 25), axis=1)[:n]
    same = np.r_[False, np.abs(np.diff(v)) < 0.05]
    run = np.zeros(n)
    for i in range(1, n):
        run[i] = run[i - 1] + 1 if same[i] else 0
    hours = np.array([datetime.fromtimestamp(int(t), config.IST).hour for t in ts])
    return np.column_stack([v, d_prev, d_next, roll_std, v - med24, np.minimum(run, 48),
                            np.sin(2 * np.pi * hours / 24), np.cos(2 * np.pi * hours / 24)])


def _series(conn: sqlite3.Connection, bin_id: str, since: int, until: int):
    rs = conn.execute("SELECT ts, fill_pct, fault FROM readings WHERE bin_id=? AND source IN ('sensor','api')"
                      " AND ts BETWEEN ? AND ? AND fill_pct IS NOT NULL ORDER BY ts", (bin_id, since, until)).fetchall()
    ts = np.array([r[0] for r in rs], dtype=np.int64)
    v = np.array([r[1] for r in rs], dtype=float)
    lab = np.array([r[2] for r in rs], dtype=int)
    colls = np.array([r[0] for r in conn.execute(
        "SELECT ts FROM events WHERE bin_id=? AND kind='collected' AND ts BETWEEN ? AND ?",
        (bin_id, since - 86400, until))], dtype=np.int64)
    return ts, v, lab, colls


_IF_CACHE: dict = {}


def isolation_forest(conn: sqlite3.Connection, now_ts: int, days: int = 28):
    """Fit (or reuse) an IsolationForest on recent sensor readings of all bins."""
    from sklearn.ensemble import IsolationForest
    key = (db.data_version(conn) // 50, now_ts // 86400)
    if key in _IF_CACHE:
        return _IF_CACHE[key]
    X = []
    for b in db.bins(conn):
        if not b["has_sensor"]:
            continue
        ts, v, _, _ = _series(conn, b["id"], now_ts - days * 86400, now_ts)
        if len(v) > 24:
            X.append(features(ts, v))
    if not X:
        return None
    model = IsolationForest(n_estimators=150, contamination=0.03, random_state=0).fit(np.vstack(X))
    _IF_CACHE.clear()
    _IF_CACHE[key] = model
    return model


def detect(conn: sqlite3.Connection, bin_id: str, since: int, until: int, model=None) -> dict:
    ts, v, lab, colls = _series(conn, bin_id, since, until)
    if len(v) == 0:
        return {"ts": ts, "v": v, "label": lab, "rules": {}, "any_rule": np.zeros(0, bool), "iforest": np.zeros(0, bool)}
    rules = rule_flags(ts, v, colls)
    any_rule = np.zeros(len(v), dtype=bool)
    for k in RULES:
        any_rule |= rules[k]
    iso = np.zeros(len(v), dtype=bool)
    if model is not None and len(v) > 3:
        iso = model.predict(features(ts, v)) == -1
    return {"ts": ts, "v": v, "label": lab, "rules": rules, "any_rule": any_rule, "iforest": iso}


def sensor_health(conn: sqlite3.Connection, now_ts: int, window_h: int = 24) -> dict[str, dict]:
    """Per sensor bin: status ok / faulty / offline and why."""
    model = isolation_forest(conn, now_ts)
    out = {}
    for b in db.bins(conn):
        if not b["has_sensor"]:
            continue
        d = detect(conn, b["id"], now_ts - window_h * 3600, now_ts, model)
        last = int(d["ts"][-1]) if len(d["ts"]) else None
        reasons = [k for k in RULES if len(d["v"]) and d["rules"][k].sum() >= (3 if k != "no_drop" else 1)]
        iso_frac = float(d["iforest"].mean()) if len(d["v"]) else 0.0
        if iso_frac >= 0.35 and len(d["v"]) >= 6:
            reasons.append("unusual pattern (IsolationForest)")
        if last is None or now_ts - last > 6 * 3600:
            status = "offline"
            reasons = ["no reading for 6+ hours"]
        else:
            status = "faulty" if reasons else "ok"
        out[b["id"]] = {"status": status, "reasons": reasons, "last_reading_ts": last,
                        "iforest_fraction": round(iso_frac, 2)}
    return out


def faulty_sensors(conn: sqlite3.Connection, now_ts: int) -> set[str]:
    return {b for b, h in sensor_health(conn, now_ts).items() if h["status"] != "ok"}


def surges(conn: sqlite3.Connection, now_ts: int, hours: int = 6, ratio: float = 1.6,
           min_rise: float = 15.0) -> list[dict]:
    """Bins whose fill rose much faster over the last hours than their normal pattern."""
    since = now_ts - hours * 3600
    prof = state.rate_profiles(conn, since)
    out = []
    for b in db.bins(conn):
        rs = conn.execute("SELECT ts, fill_pct FROM readings WHERE bin_id=? AND ts BETWEEN ? AND ?"
                          " AND fill_pct IS NOT NULL AND source IN ('sensor','api','supervisor','crew','complaint')"
                          " ORDER BY ts", (b["id"], since, now_ts)).fetchall()
        if len(rs) < 2:
            continue
        coll = conn.execute("SELECT COUNT(*) FROM events WHERE bin_id=? AND kind='collected' AND ts BETWEEN ? AND ?",
                            (b["id"], rs[0][0], rs[-1][0])).fetchone()[0]
        if coll:
            continue
        observed = rs[-1][1] - rs[0][1]
        expected = state.growth_between(prof[b["id"]], rs[0][0], rs[-1][0])
        if observed >= min_rise and observed > ratio * max(expected, 1.0):
            out.append({"bin_id": b["id"], "landmark": b["landmark"], "observed_rise": round(observed, 1),
                        "expected_rise": round(expected, 1), "ratio": round(observed / max(expected, 1.0), 1),
                        "since_ts": rs[0][0]})
    return sorted(out, key=lambda x: -x["ratio"])


def evaluate_synthetic(conn: sqlite3.Connection, days: int = 60) -> dict:
    """Precision / recall against the generator's fault labels (synthetic data only)."""
    info_end = conn.execute("SELECT MAX(ts) FROM readings WHERE synthetic=1").fetchone()[0]
    since = info_end - days * 86400
    model = isolation_forest(conn, info_end, days=28)
    tot = {k: [0, 0, 0] for k in ("rules", "iforest", "combined")}   # tp, fp, fn
    by_type = {1: [0, 0], 2: [0, 0], 3: [0, 0]}                       # caught, total (combined)
    for b in db.bins(conn):
        if not b["has_sensor"]:
            continue
        d = detect(conn, b["id"], since, info_end, model)
        y = d["label"] > 0
        comb = d["any_rule"] | d["iforest"]
        for code in by_type:
            m = d["label"] == code
            by_type[code][0] += int((comb & m).sum())
            by_type[code][1] += int(m.sum())
        for name, pred in (("rules", d["any_rule"]), ("iforest", d["iforest"]),
                           ("combined", d["any_rule"] | d["iforest"])):
            tot[name][0] += int((pred & y).sum())
            tot[name][1] += int((pred & ~y).sum())
            tot[name][2] += int((~pred & y).sum())
    out = {"label": "evaluated on SYNTHETIC fault labels", "days": days,
           "recall_by_type": {name: round(by_type[c][0] / max(by_type[c][1], 1), 3)
                              for c, name in ((1, "stuck"), (2, "drift"), (3, "spike"))}}
    for name, (tp, fp, fn) in tot.items():
        out[name] = {"precision": round(tp / max(tp + fp, 1), 3), "recall": round(tp / max(tp + fn, 1), 3)}
    return out
