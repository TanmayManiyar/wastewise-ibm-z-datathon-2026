"""Planned vs actual: what the crews did with today's approved plan(s)."""
from __future__ import annotations

import csv
import io
import json
import sqlite3

from . import audit, db, planner

ACTIONS = {
    "late": "Collected late - expect complaints; ask the crew what delayed them.",
    "could_not_reach": "Send the spare vehicle or the nearest truck; check road access (parked cars, works).",
    "bin_missing": "Supervisor to inspect; arrange a replacement bin and report theft or damage.",
    "overdue": "Call the crew. If the truck is delayed, use Re-plan from now.",
    "missed": "Not collected this shift: put it first on tomorrow's plan or send an extra trip today.",
}
DONE_KINDS = ("collected", "could_not_reach", "bin_missing")


def day_plans(conn: sqlite3.Connection, day: str, round_: str | None = None) -> list[dict]:
    """Plans that were approved for the day (oldest first, including those later replaced).
    round_: None = all, "main" = the regular shift, "extra" = the evening round."""
    kinds = {"main": "('daily','replan')", "extra": "('extra')"}.get(round_, "('daily','replan','extra')")
    ids = [r[0] for r in conn.execute(f"SELECT id FROM plans WHERE plan_date=? AND approved_at IS NOT NULL"
                                      f" AND kind IN {kinds} ORDER BY id", (day,))]
    return [planner.get(conn, i) for i in ids]


def _round(plan: dict) -> str:
    return "extra" if plan.get("kind") == "extra" else "main"


def _window(conn: sqlite3.Connection, day: str, round_: str) -> tuple[int, int]:
    """Time window in which crew updates belong to this round."""
    settings = db.get_settings(conn)
    shift_start, shift_end = planner.shift_window(settings, __import__("datetime").date.fromisoformat(day))
    extras = day_plans(conn, day, "extra")
    if round_ == "extra":
        if not extras:
            return shift_end, shift_end
        return min(p["start_ts"] for p in extras) - 1800, max(p["shift_end_ts"] for p in extras) + 4 * 3600
    end = (min(p["start_ts"] for p in extras) - 1800) if extras else shift_end + 4 * 3600
    return shift_start - 3600, end


def planned_stops(plans: list[dict]) -> dict[str, dict]:
    """Bin -> its latest planned visit (later re-plans override earlier plans)."""
    out: dict[str, dict] = {}
    for p in plans:
        for r in p["routes"]:
            for s in r["stops"]:
                if s["kind"] == "bin":
                    out[s["bin_id"]] = {"bin_id": s["bin_id"], "landmark": s["landmark"], "vehicle_id": r["vehicle_id"],
                                        "vehicle_name": r["vehicle_name"], "planned_ts": s["arrive_ts"],
                                        "plan_id": p["id"], "expected_fill": s["expected_fill_pct"]}
    return out


def status_rows(conn: sqlite3.Connection, day: str, now_ts: int, late_min: int | None = None) -> list[dict]:
    settings = db.get_settings(conn)
    late_min = late_min if late_min is not None else int(settings["late_threshold_min"])
    rows = []
    for round_ in ("main", "extra"):
        plans = day_plans(conn, day, round_)
        if not plans:
            continue
        lo, hi = _window(conn, day, round_)
        round_end = (max(p["shift_end_ts"] for p in plans) if round_ == "extra"
                     else planner.shift_window(settings, __import__("datetime").date.fromisoformat(day))[1])
        ids = [p["id"] for p in plans]
        planned = planned_stops(plans)
        for bid, p in sorted(planned.items(), key=lambda kv: (kv[1]["vehicle_id"], kv[1]["planned_ts"])):
            # A crew update counts if it belongs to one of this round's plans (even when tapped before the
            # shift, e.g. in a rehearsal) or was made during this round's time window.
            e = db.one(conn, "SELECT * FROM events WHERE bin_id=? AND kind IN ('collected','could_not_reach','bin_missing')"
                             f" AND (plan_id IN ({','.join('?' * len(ids))}) OR ts BETWEEN ? AND ?) ORDER BY ts DESC LIMIT 1",
                       (bid, *ids, lo, hi))
            row = dict(p, actual_ts=None, fill_reported=None, delay_min=None, by=None,
                       round="Evening" if round_ == "extra" else "Morning")
            if e:
                row.update(actual_ts=e["ts"], fill_reported=e["fill_before"], by=e["user"])
                row["delay_min"] = round((e["ts"] - p["planned_ts"]) / 60)
                if e["kind"] == "collected":
                    row["status"] = "late" if row["delay_min"] > late_min else "collected"
                else:
                    row["status"] = e["kind"]
            elif now_ts >= round_end:
                row["status"] = "missed"
            elif now_ts > p["planned_ts"] + late_min * 60:
                row["status"] = "overdue"
                row["delay_min"] = round((now_ts - p["planned_ts"]) / 60)
            else:
                row["status"] = "pending"
            row["action"] = ACTIONS.get(row["status"], "")
            rows.append(row)
    return rows


def summary(rows: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in rows:
        v = out.setdefault(r["vehicle_name"], {"planned": 0, "collected": 0, "late": 0, "problems": 0, "missed": 0,
                                               "overdue": 0, "pending": 0})
        v["planned"] += 1
        s = r["status"]
        if s in ("collected", "late"):
            v["collected"] += 1
        if s == "late":
            v["late"] += 1
        if s in ("could_not_reach", "bin_missing"):
            v["problems"] += 1
        if s in ("missed", "overdue", "pending"):
            v[s] += 1
    return out


def to_csv(rows: list[dict], fmt_time) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["vehicle", "bin", "place", "planned", "actual", "status", "delay_min", "fill_reported_pct",
                "recorded_by", "what_to_do"])
    for r in rows:
        w.writerow([r["vehicle_name"], r["bin_id"], r["landmark"], fmt_time(r["planned_ts"]),
                    fmt_time(r["actual_ts"]) if r["actual_ts"] else "", r["status"], r["delay_min"] or "",
                    r["fill_reported"] if r["fill_reported"] is not None else "", r["by"] or "", r["action"]])
    return buf.getvalue()


# ------------------------------------------------------------------ crew side
def route_for(conn: sqlite3.Connection, day: str, vehicle_id: str) -> tuple[dict | None, dict | None]:
    """The crew's current route: the morning route until it is finished, then the approved evening round."""
    out = (None, None)
    for round_ in ("main", "extra"):
        p = planner.current(conn, day, round_=round_)
        if not p or p["status"] != "approved":
            continue
        r = next((x for x in p["routes"] if x["vehicle_id"] == vehicle_id and x["stops"]), None)
        if r is None:
            continue
        if round_ == "main" and all(s_ == "done" for s_, stp in zip(stop_states(conn, p, r), r["stops"])
                                     if stp["kind"] == "bin"):
            out = (p, r)       # morning finished: show the evening route if there is one
            continue
        return p, r
    return out


def stop_states(conn: sqlite3.Connection, plan: dict, route: dict) -> list[str]:
    """'done' / 'todo' for each stop of the route, from crew updates."""
    since, _ = _window(conn, plan["plan_date"], _round(plan))
    if _round(plan) == "extra":
        since = plan["start_ts"] - 1800
    ids = [p["id"] for p in day_plans(conn, plan["plan_date"], _round(plan))] or [plan["id"]]
    if plan["id"] not in ids:
        ids.append(plan["id"])
    unloads = conn.execute("SELECT COUNT(*) FROM events WHERE kind='landfill_unload' AND vehicle_id=? AND plan_id=?",
                           (route["vehicle_id"], plan["id"])).fetchone()[0]
    out, lf_seen = [], 0
    for s in route["stops"]:
        if s["kind"] == "bin":
            e = conn.execute("SELECT 1 FROM events WHERE bin_id=? AND kind IN ('collected','could_not_reach','bin_missing')"
                             f" AND (plan_id IN ({','.join('?' * len(ids))}) OR ts>=?) LIMIT 1",
                             (s["bin_id"], *ids, since)).fetchone()
            out.append("done" if e else "todo")
        elif s["kind"] == "landfill":
            lf_seen += 1
            out.append("done" if lf_seen <= unloads else "todo")
        else:
            out.append("todo")
    return out


def load_on_board(conn: sqlite3.Connection, plan: dict, route: dict, bins: dict[str, dict]) -> float:
    """Litres on the truck since its last unload, from what the crew reported."""
    last_unload = conn.execute("SELECT MAX(ts) FROM events WHERE kind='landfill_unload' AND vehicle_id=? AND plan_id=?",
                               (route["vehicle_id"], plan["id"])).fetchone()[0]
    since = last_unload or plan["start_ts"] - 3600
    load = 0.0 if last_unload else float(route.get("start_load_l", 0))
    for e in conn.execute("SELECT bin_id, fill_before FROM events WHERE kind='collected' AND vehicle_id=? AND plan_id=?"
                          " AND ts>?", (route["vehicle_id"], plan["id"], since)):
        b = bins.get(e[0])
        if b:
            load += b["capacity_l"] * min(e[1] if e[1] is not None else 80.0, 120.0) / 100
    return load


def record_crew(conn: sqlite3.Connection, plan: dict, vehicle_id: str, kind: str, user: str,
                bin_id: str | None = None, fill: float | None = None, ts: int | None = None,
                note: str | None = None) -> None:
    ts = ts or db.now()
    with db.tx(conn):
        db.add_event(conn, kind, bin_id, vehicle_id, plan["id"], fill_before=fill, ts=ts, user=user, note=note)
        if kind == "collected" and bin_id:
            db.add_reading(conn, bin_id, 2.0, "crew", ts=ts + 1, user=user, note="emptied")
        if kind == "could_not_reach" and bin_id and fill is not None:
            db.add_reading(conn, bin_id, fill, "crew", ts=ts, user=user, note="seen, not collected")
        audit.log(conn, user, f"crew_{kind}", {"plan_id": plan["id"], "vehicle": vehicle_id, "bin_id": bin_id,
                                               "fill": fill, "note": note})
