"""Truck log: fuel, maintenance and breakdowns (Settings > Vehicles & depot).

Each entry can take the truck out of service for a period ("in the workshop until Monday 10:00").
Planning leaves out trucks that are out of service when the shift starts and says so in the plan.
"""
from __future__ import annotations

import sqlite3

from . import audit, db, fmt

KINDS = {"fuel": "Fuel", "maintenance": "Maintenance", "breakdown": "Breakdown", "repair_done": "Back in service"}


def add(conn: sqlite3.Connection, vehicle_id: str, kind: str, user: str, litres: float | None = None,
        cost_rs: float | None = None, odometer_km: float | None = None, note: str = "",
        out_from: int | None = None, out_until: int | None = None, ts: int | None = None) -> int:
    if kind not in KINDS:
        raise ValueError(kind)
    if out_until is not None and out_from is not None and out_until <= out_from:
        raise ValueError("'Out of service until' must be after the start")
    with db.tx(conn):
        cur = conn.execute("INSERT INTO vehicle_log(vehicle_id, ts, kind, litres, cost_rs, odometer_km, note,"
                           " out_from, out_until, user) VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (vehicle_id, ts or db.now(), kind, litres, cost_rs, odometer_km, note, out_from,
                            out_until, user))
        if kind == "repair_done":     # back in service now: end any open out-of-service period
            conn.execute("UPDATE vehicle_log SET out_until=? WHERE vehicle_id=? AND out_until>?",
                         (ts or db.now(), vehicle_id, ts or db.now()))
        audit.log(conn, user, "vehicle_log", {"vehicle": vehicle_id, "kind": kind, "litres": litres,
                                              "out_until": fmt.when(out_until) if out_until else None})
        db.bump_version(conn)
    return int(cur.lastrowid)


def entries(conn: sqlite3.Connection, vehicle_id: str | None = None, limit: int = 100) -> list[dict]:
    q = "SELECT * FROM vehicle_log" + (" WHERE vehicle_id=?" if vehicle_id else "") + " ORDER BY ts DESC LIMIT ?"
    return db.rows(conn, q, ((vehicle_id, limit) if vehicle_id else (limit,)))


def out_of_service(conn: sqlite3.Connection, at_ts: int, until_ts: int | None = None) -> dict[str, dict]:
    """vehicle_id -> log entry, for trucks out of service at any time in [at_ts, until_ts]."""
    until_ts = until_ts or at_ts
    out = {}
    for r in db.rows(conn, "SELECT * FROM vehicle_log WHERE out_from IS NOT NULL AND out_from<=? AND"
                           " (out_until IS NULL OR out_until>?) ORDER BY ts", (until_ts, at_ts)):
        out[r["vehicle_id"]] = r
    return out


def reason_text(r: dict) -> str:
    until = f"until {fmt.when(r['out_until'])}" if r.get("out_until") else "until further notice"
    return f"{KINDS.get(r['kind'], r['kind']).lower()}" + (f" ({r['note']})" if r.get("note") else "") + f", {until}"


def month_totals(conn: sqlite3.Connection, start_ts: int, end_ts: int) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in db.rows(conn, "SELECT vehicle_id, kind, litres, cost_rs FROM vehicle_log WHERE ts BETWEEN ? AND ?",
                     (start_ts, end_ts)):
        t = out.setdefault(r["vehicle_id"], {"fuel_l": 0.0, "cost_rs": 0.0, "breakdowns": 0, "maintenance": 0})
        t["fuel_l"] += r["litres"] or 0
        t["cost_rs"] += r["cost_rs"] or 0
        t["breakdowns"] += r["kind"] == "breakdown"
        t["maintenance"] += r["kind"] == "maintenance"
    return out
