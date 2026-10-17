"""Calculations behind the Reports page (all from the database; no UI code)."""
from __future__ import annotations

import glob
import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

from . import calendar_in, config, db, forecast, state


def _day(ts: int) -> date:
    return datetime.fromtimestamp(ts, config.IST).date()


def bin_sizing(conn: sqlite3.Connection, now_ts: int, days: int = 28) -> list[dict]:
    """How full bins were when collected. Recommends a bigger bin / extra pickup or less frequent service."""
    since = now_ts - days * 86400
    out = []
    for b in db.bins(conn):
        rows = [r[0] for r in conn.execute("SELECT fill_before FROM events WHERE bin_id=? AND kind='collected'"
                                           " AND fill_before IS NOT NULL AND ts BETWEEN ? AND ?",
                                           (b["id"], since, now_ts))]
        full_obs = conn.execute("SELECT COUNT(*) FROM readings WHERE bin_id=? AND fill_pct>=99 AND ts BETWEEN ? AND ?"
                                " AND source IN ('sensor','api','supervisor','complaint')",
                                (b["id"], since, now_ts)).fetchone()[0]
        n = len(rows)
        mean = float(np.mean(rows)) if rows else None
        share_full = float(np.mean([r >= 100 for r in rows])) if rows else None
        full_hours_per_day = full_obs / days if b["has_sensor"] else 0.0
        if n < 5:
            rec = "not enough collections to judge"
        elif share_full >= 0.6 or full_hours_per_day >= 2.5:
            rec = "too small: give a bigger bin or an extra pickup"
        elif mean < 40 and b["collect_every_days"] == 1:
            rec = "over-serviced: collect every 2 days"
        elif mean < 30 and b["collect_every_days"] >= 2:
            rec = "over-serviced: a smaller bin would do"
        else:
            rec = "right size"
        out.append({"bin_id": b["id"], "landmark": b["landmark"], "site_type": b["site_type"],
                    "capacity_l": b["capacity_l"], "every_days": b["collect_every_days"], "collections": n,
                    "mean_fill_at_collection": None if mean is None else round(mean, 1),
                    "share_full_at_collection": None if share_full is None else round(share_full, 2),
                    "full_readings": full_obs, "recommendation": rec})
    return out


def patterns(conn: sqlite3.Connection, now_ts: int) -> dict[str, np.ndarray]:
    """Average filling (% of capacity per hour) by weekday x hour, per site type."""
    prof = state.rate_profiles(conn, now_ts)
    by_type: dict[str, list[np.ndarray]] = {}
    for b in db.bins(conn):
        by_type.setdefault(b["site_type"], []).append(prof[b["id"]])
    return {k: np.mean(v, axis=0) for k, v in by_type.items()}


def daily_volume(conn: sqlite3.Connection, now_ts: int, days: int = 180) -> list[dict]:
    """Litres collected per day (crew estimates x bin capacity), with festival and monsoon flags."""
    cap = {b["id"]: b["capacity_l"] for b in db.bins(conn, active_only=False)}
    vol: dict[date, float] = {}
    for bid, ts, f in conn.execute("SELECT bin_id, ts, fill_before FROM events WHERE kind='collected'"
                                   " AND fill_before IS NOT NULL AND ts BETWEEN ? AND ?",
                                   (now_ts - days * 86400, now_ts)):
        d = _day(ts)
        vol[d] = vol.get(d, 0.0) + cap.get(bid, 0) * min(f, 120) / 100
    out = []
    for d in sorted(vol):
        f = calendar_in.festival_on(d - timedelta(days=1))   # collected in the morning = waste of the day before
        out.append({"date": d.isoformat(), "litres": round(vol[d]), "festival": f[0] if f else None,
                    "monsoon": calendar_in.is_monsoon(d)})
    return out


def festival_effect(rows: list[dict]) -> list[dict]:
    normal = [r["litres"] for r in rows if not r["festival"]]
    base = float(np.median(normal)) if normal else None
    out = []
    for name in sorted({r["festival"] for r in rows if r["festival"]}):
        v = [r["litres"] for r in rows if r["festival"] == name]
        out.append({"festival": name, "days": len(v), "mean_litres": round(float(np.mean(v))),
                    "vs_normal": round(float(np.mean(v)) / base, 2) if base else None})
    return out


def outlook(conn: sqlite3.Connection, now_ts: int, days: int = 7) -> list[dict]:
    """Expected waste for the next days (learned weekly pattern x festival uplift seen in history)
    against what the fleet can carry in one shift."""
    prof = state.rate_profiles(conn, now_ts)
    bins = db.bins(conn)
    hist = daily_volume(conn, now_ts)
    fe = {f["festival"]: f["vs_normal"] for f in festival_effect(hist) if f["vs_normal"]}
    trips = 2.0
    p = conn.execute("SELECT body FROM plans WHERE approved_at IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
    if p:
        body = json.loads(p[0])
        used = [r["totals"]["landfill_trips"] for r in body["routes"] if r["totals"]["bins"]]
        trips = max(1.0, float(np.mean(used))) if used else trips
    fleet = sum(v["capacity_l"] for v in db.vehicles(conn)) * 0.95 * trips
    # Sensor bins: the ML forecaster's next 24 hours (run on the Telum / CPU engine chosen for this machine),
    # carried to the following days with each bin's weekly pattern. Other bins: the learned weekly pattern.
    ml_scale = {}
    try:
        ml = forecast.predict_rates(conn, now_ts, 24, [b for b in bins if b["has_sensor"]], {}, job="7-day outlook")
    except Exception:
        ml = None
    t1 = _day(now_ts) + timedelta(days=1)
    if ml:
        for b in bins:
            r = ml["rates"].get(b["id"])
            base = float(prof[b["id"]][t1.weekday()].sum())
            if r is not None and base > 0 and b["has_sensor"]:
                ml_scale[b["id"]] = float(np.clip(np.sum(r[:24]) / base, 0.5, 2.0))
    out = []
    today = _day(now_ts)
    for k in range(1, days + 1):
        d = today + timedelta(days=k)
        dow = d.weekday()
        litres = sum(float(prof[b["id"]][dow].sum()) * ml_scale.get(b["id"], 1.0) * b["capacity_l"] / 100
                     for b in bins)
        f = calendar_in.festival_on(d)
        uplift = fe.get(f[0], 1.0) if f else 1.0
        out.append({"date": d.isoformat(), "expected_litres": round(litres * uplift),
                    "festival": f[0] if f else None, "uplift": uplift, "fleet_capacity_litres": round(fleet),
                    "over_capacity": litres * uplift > fleet,
                    "source": "ML forecaster for sensor bins + weekly pattern" if ml_scale else "weekly pattern"})
    return out


def savings(conn: sqlite3.Connection) -> dict:
    """Approved plans vs the nearest-bin routine on the same day and data."""
    rows = []
    for (body,) in conn.execute("SELECT body FROM plans WHERE approved_at IS NOT NULL AND kind='daily'"):
        p = json.loads(body)
        rows.append((p["plan_date"], p["kpis"], p["baseline"], bool(p.get("label"))))
    keys = ("distance_km", "fuel_l", "co2_kg", "expected_overflow_min", "bins_full_on_arrival")
    tot = {k: [0.0, 0.0] for k in keys}
    for _, k, b, _ in rows:
        for x in keys:
            tot[x][0] += k[x]
            tot[x][1] += b[x]
    return {"plans": len(rows), "demonstration": any(r[3] for r in rows),
            "totals": {k: {"wastewise": round(v[0], 1), "routine": round(v[1], 1), "saved": round(v[1] - v[0], 1)}
                       for k, v in tot.items()},
            "per_plan": [{"date": d, "km_wastewise": k["distance_km"], "km_routine": b["distance_km"],
                          "overflow_wastewise": k["expected_overflow_min"], "overflow_routine": b["expected_overflow_min"]}
                         for d, k, b, _ in rows]}


def forecast_accuracy(conn: sqlite3.Connection) -> dict:
    cur = forecast.promoted(conn)
    out = {"promoted": None, "mildura": None}
    if cur:
        out["promoted"] = {"id": cur["id"], "created_at": cur["created_at"], "metrics": cur["metrics"]}
    files = sorted(glob.glob(str(config.ROOT / "evidence" / "models_*.json")))
    for f in files[::-1]:
        d = json.loads(Path(f).read_text())
        if d.get("mildura"):
            out["mildura"] = d["mildura"]
            break
    return out


# ------------------------------------------------------------------ crew performance and monthly summary
def crew_performance(conn: sqlite3.Connection, now_ts: int, days: int = 28) -> list[dict]:
    """Per truck over the last `days` days: on-time %, missed %, problems, proof of service.
    A visit is on time when collected within the 'late after' minutes of its planned time (routine or plan).
    Missed = could not reach, or planned in an approved plan and never recorded."""
    from . import progress
    late_s = 60 * int(db.get_settings(conn)["late_threshold_min"])
    since = now_ts - days * 86400
    names = {v["id"]: v["name"] for v in db.vehicles(conn, active_only=False)}
    out: dict[str, dict] = {vid: {"vehicle_id": vid, "truck": names[vid], "visits": 0, "collected": 0, "on_time": 0,
                                  "not_reached": 0, "missing": 0, "never_recorded": 0, "with_gps": 0, "verified": 0,
                                  "synthetic": 0, "timed": 0} for vid in names}
    for e in db.rows(conn, "SELECT * FROM events WHERE ts BETWEEN ? AND ? AND vehicle_id IS NOT NULL AND kind IN"
                           " ('collected','could_not_reach','bin_missing')", (since, now_ts)):
        t = out.get(e["vehicle_id"])
        if t is None:
            continue
        t["visits"] += 1
        t["synthetic"] += e["synthetic"]
        if e["kind"] == "collected":
            t["collected"] += 1
            if e["planned_ts"] is not None:
                t["timed"] += 1
                t["on_time"] += e["ts"] - e["planned_ts"] <= late_s
        elif e["kind"] == "could_not_reach":
            t["not_reached"] += 1
        else:
            t["missing"] += 1
        if e["dist_m"] is not None:
            t["with_gps"] += 1
            t["verified"] += e["dist_m"] <= 100
    # stops in approved plans that were never recorded
    days_with_plans = [r[0] for r in conn.execute("SELECT DISTINCT plan_date FROM plans WHERE approved_at IS NOT NULL"
                                                  " AND plan_date BETWEEN ? AND ?",
                                                  (_day(since).isoformat(), _day(now_ts).isoformat()))]
    for d in days_with_plans:
        for r in progress.status_rows(conn, d, now_ts):
            if r["status"] == "missed" and r["vehicle_id"] in out:
                out[r["vehicle_id"]]["never_recorded"] += 1
                out[r["vehicle_id"]]["visits"] += 1
    rows = []
    for t in out.values():
        v = max(t["visits"], 1)
        rows.append(dict(t, on_time_pct=round(100 * t["on_time"] / t["timed"]) if t["timed"] else None,
                         missed_pct=round(100 * (t["not_reached"] + t["never_recorded"]) / v, 1) if t["visits"] else None,
                         problems=t["not_reached"] + t["missing"],
                         verified_pct=round(100 * t["verified"] / t["with_gps"]) if t["with_gps"] else None))
    return rows


def monthly_summary(conn: sqlite3.Connection, year: int, month: int) -> dict:
    """The monthly figures the ward reports to NMMC headquarters."""
    from . import fleet
    start = datetime(year, month, 1, tzinfo=config.IST)
    end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=config.IST)
    s, e = int(start.timestamp()), int(end.timestamp()) - 1
    bins = {b["id"]: b for b in db.bins(conn, active_only=False)}
    days: dict[str, dict] = {}
    for ev in db.rows(conn, "SELECT ts, bin_id, kind, fill_before, synthetic FROM events WHERE ts BETWEEN ? AND ?"
                            " AND kind IN ('collected','could_not_reach','bin_missing')", (s, e)):
        d = _day(ev["ts"]).isoformat()
        r = days.setdefault(d, {"date": d, "collections": 0, "litres": 0.0, "not_reached": 0, "missing": 0,
                                "complaints": 0, "complaints_closed": 0, "km_planned": 0.0, "synthetic_rows": 0})
        r["synthetic_rows"] += ev["synthetic"]
        if ev["kind"] == "collected":
            r["collections"] += 1
            b = bins.get(ev["bin_id"])
            if b:
                r["litres"] += b["capacity_l"] * min(ev["fill_before"] if ev["fill_before"] is not None else 75, 120) / 100
        elif ev["kind"] == "could_not_reach":
            r["not_reached"] += 1
        else:
            r["missing"] += 1
    close_h = []
    for c in db.rows(conn, "SELECT ts, status, resolved_at FROM complaints WHERE ts BETWEEN ? AND ?", (s, e)):
        d = _day(c["ts"]).isoformat()
        r = days.setdefault(d, {"date": d, "collections": 0, "litres": 0.0, "not_reached": 0, "missing": 0,
                                "complaints": 0, "complaints_closed": 0, "km_planned": 0.0, "synthetic_rows": 0})
        r["complaints"] += 1
        if c["status"] == "resolved" and c["resolved_at"]:
            r["complaints_closed"] += 1
            close_h.append((c["resolved_at"] - c["ts"]) / 3600)
    for p in db.rows(conn, "SELECT plan_date, body FROM plans WHERE approved_at IS NOT NULL AND kind IN"
                           " ('daily','replan','extra') AND status='approved' AND plan_date BETWEEN ? AND ?",
                     (start.date().isoformat(), (end.date() - timedelta(days=1)).isoformat())):
        body = json.loads(p["body"])
        r = days.setdefault(p["plan_date"], {"date": p["plan_date"], "collections": 0, "litres": 0.0, "not_reached": 0,
                                             "missing": 0, "complaints": 0, "complaints_closed": 0, "km_planned": 0.0,
                                             "synthetic_rows": 0})
        r["km_planned"] += body["kpis"]["distance_km"]
    rows = [days[d] for d in sorted(days)]
    fuel = fleet.month_totals(conn, s, e)
    tot = {k: sum(r[k] for r in rows) for k in ("collections", "litres", "not_reached", "missing", "complaints",
                                                "complaints_closed", "km_planned")}
    return {"year": year, "month": month, "label": start.strftime("%B %Y"), "days": rows, "totals": tot,
            "median_close_h": round(float(np.median(close_h)), 1) if close_h else None,
            "fuel": fuel, "performance": crew_performance(conn, min(e, db.now()), days=(min(e, db.now()) - s) // 86400 + 1),
            "synthetic": any(r["synthetic_rows"] for r in rows), "start_ts": s, "end_ts": e}


def monthly_csv(m: dict) -> str:
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "collections", "litres_collected", "could_not_reach", "bin_missing", "complaints",
                "complaints_closed", "km_in_approved_plans", "contains_synthetic_rows"])
    for r in m["days"]:
        w.writerow([r["date"], r["collections"], round(r["litres"]), r["not_reached"], r["missing"], r["complaints"],
                    r["complaints_closed"], round(r["km_planned"], 1), int(r["synthetic_rows"] > 0)])
    return buf.getvalue()
