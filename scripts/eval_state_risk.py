"""Evaluate the fill-state estimate and overflow-risk calibration against the
synthetic ground truth (demonstration data only - not real-world accuracy).

Usage: python scripts/eval_state_risk.py [--days 14] [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from wastewise import config, db, risk, state  # noqa: E402


def evaluate(conn, days: int = 14) -> dict:
    end = json.loads(db.get_meta(conn, "synthetic_info"))["end_ts"]
    bins = {b["id"]: b for b in db.bins(conn)}
    err = {"sensor": [], "manual": []}
    P, Y = [], []
    for k in range(1, days + 1):
        d = (datetime.fromtimestamp(end, config.IST) - timedelta(days=k)).replace(hour=6, minute=0)
        at = int(d.timestamp())
        prof = state.rate_profiles(conn, at)
        st = state.estimate(conn, at, prof)
        truth = dict(conn.execute("SELECT bin_id, fill_pct FROM synthetic_truth WHERE ts = "
                                  "(SELECT MAX(ts) FROM synthetic_truth WHERE ts <= ?)", (at,)).fetchall())
        for b, s in st.items():
            err["sensor" if bins[b]["has_sensor"] else "manual"].append(s.fill - truth[b])
        ids = sorted(st)
        rates = np.array([state.profile_rates(prof[b], at, 6) for b in ids])
        rc = risk.build(ids, np.array([st[b].fill for b in ids]), np.array([st[b].sd for b in ids]), rates, at, 240)
        for i, b in enumerate(ids):
            stop = conn.execute("SELECT MIN(ts) FROM events WHERE bin_id=? AND ts>=? AND kind IN"
                                " ('collected','could_not_reach')", (b, at)).fetchone()[0] or at + 86400
            for ts, f in conn.execute("SELECT ts, fill_pct FROM synthetic_truth WHERE bin_id=? AND ts BETWEEN ? AND ?",
                                      (b, at, min(stop - 1, at + 4 * 3600))):
                P.append(rc.p_full_at(i, ts))
                Y.append(float(f >= 100))
    P, Y = np.array(P), np.array(Y)
    out = {"label": "evaluated on SYNTHETIC demonstration data", "days": days}
    for name, e in err.items():
        e = np.array(e)
        out[f"state_mae_{name}"] = round(float(np.abs(e).mean()), 2)
        out[f"state_bias_{name}"] = round(float(e.mean()), 2)
    out["risk_n"] = int(len(P))
    out["risk_base_rate"] = round(float(Y.mean()), 3)
    out["brier_model"] = round(float(((P - Y) ** 2).mean()), 4)
    out["brier_climatology"] = round(float(((Y.mean() - Y) ** 2).mean()), 4)
    cal = []
    for lo, hi in [(0, .1), (.1, .3), (.3, .5), (.5, .7), (.7, .9), (.9, 1.01)]:
        m = (P >= lo) & (P < hi)
        if m.sum():
            cal.append({"bin": f"{lo:.1f}-{min(hi, 1):.1f}", "n": int(m.sum()),
                        "predicted": round(float(P[m].mean()), 2), "observed": round(float(Y[m].mean()), 2)})
    out["calibration"] = cal
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--json")
    a = ap.parse_args()
    res = evaluate(db.connect(), a.days)
    print(json.dumps(res, indent=2))
    if a.json:
        Path(a.json).write_text(json.dumps(res, indent=2))
