"""Who is on each truck.

A crew account (crew1, crew2, ...) is shared by everyone on a truck, so the account alone does not say who
did the work. The administrator keeps a list of crew members; at the start of the shift the crew phone asks
"Who is on this truck today?" and "Who is using this phone?". Every tap and photo then stores that person's
name next to the account (events.person), and the route sheets and reports show the names.
"""
from __future__ import annotations

import json
import sqlite3

from . import audit, db

# Demonstration roster (clearly labelled): only created in demonstration mode when the list is empty.
DEMO_NAMES = {
    "TRK-1": ["Ramesh Patil (demo)", "Sunil Kamble (demo)", "Anita Jadhav (demo)"],
    "TRK-2": ["Mahesh Shinde (demo)", "Prakash More (demo)", "Savita Gaikwad (demo)"],
    "TRK-3": ["Vijay Pawar (demo)", "Deepak Salunkhe (demo)", "Lata Bhosale (demo)"],
}


def members(conn: sqlite3.Connection, active_only: bool = True) -> list[dict]:
    return db.rows(conn, "SELECT * FROM crew_members" + (" WHERE active=1" if active_only else "")
                   + " ORDER BY vehicle_id, name")


def add_member(conn: sqlite3.Connection, name: str, vehicle_id: str | None, actor: str) -> int:
    name = " ".join(name.split())
    if not name:
        raise ValueError("Type the person's name")
    if db.one(conn, "SELECT 1 FROM crew_members WHERE lower(name)=lower(?) AND active=1", (name,)):
        raise ValueError(f"{name} is already on the list")
    with db.tx(conn):
        cur = conn.execute("INSERT INTO crew_members(name, vehicle_id, active, created_at) VALUES (?,?,1,?)",
                           (name, vehicle_id, db.now()))
        audit.log(conn, actor, "crew_member_added", {"name": name, "vehicle": vehicle_id})
    return int(cur.lastrowid)


def set_active(conn: sqlite3.Connection, member_id: int, active: bool, actor: str) -> None:
    with db.tx(conn):
        r = db.one(conn, "SELECT name FROM crew_members WHERE id=?", (member_id,))
        conn.execute("UPDATE crew_members SET active=? WHERE id=?", (int(active), member_id))
        audit.log(conn, actor, "crew_member_" + ("activated" if active else "removed"), {"name": r and r["name"]})


def seed_demo(conn: sqlite3.Connection) -> int:
    if not db.get_meta(conn, "synthetic_info") or db.one(conn, "SELECT 1 FROM crew_members LIMIT 1"):
        return 0
    n = 0
    with db.tx(conn):
        for vid, names in DEMO_NAMES.items():
            for name in names:
                conn.execute("INSERT INTO crew_members(name, vehicle_id, active, created_at) VALUES (?,?,1,?)",
                             (name, vid, db.now()))
                n += 1
        audit.log(conn, "system", "crew_members_demo_seeded", {"count": n})
    return n


def on_truck(conn: sqlite3.Connection, day: str, vehicle_id: str) -> dict | None:
    """{"members": [...], "holder": name, "set_by", "ts"} for that day and truck, or None if not asked yet."""
    r = db.one(conn, "SELECT * FROM truck_crew WHERE day=? AND vehicle_id=?", (day, vehicle_id))
    if not r:
        return None
    r["members"] = json.loads(r["members"])
    return r


def set_on_truck(conn: sqlite3.Connection, day: str, vehicle_id: str, names: list[str], holder: str | None,
                 user: str) -> None:
    if not names:
        raise ValueError("Choose at least one person")
    if holder and holder not in names:
        raise ValueError("The person using the phone must be on the truck")
    with db.tx(conn):
        conn.execute("INSERT INTO truck_crew(day, vehicle_id, members, holder, set_by, ts) VALUES (?,?,?,?,?,?)"
                     " ON CONFLICT(day, vehicle_id) DO UPDATE SET members=excluded.members, holder=excluded.holder,"
                     " set_by=excluded.set_by, ts=excluded.ts",
                     (day, vehicle_id, json.dumps(names, ensure_ascii=False), holder, user, db.now()))
        audit.log(conn, user, "truck_crew_set", {"day": day, "vehicle": vehicle_id, "members": names,
                                                 "phone": holder})
        db.bump_version(conn)


def names_for_day(conn: sqlite3.Connection, day: str) -> dict[str, str]:
    """vehicle_id -> "A, B, C" for the route sheets."""
    return {r["vehicle_id"]: ", ".join(json.loads(r["members"]))
            for r in db.rows(conn, "SELECT vehicle_id, members FROM truck_crew WHERE day=?", (day,))}
