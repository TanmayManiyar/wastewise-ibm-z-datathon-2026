"""Road closures (Settings > Roads).

A supervisor marks a street "closed until ..." (works, a festival pandal, flooding). While it is closed
the street's road segments are taken out of the road network, so travel times and routes are recomputed
without it. Bins whose kerb stop can only be reached through closed streets are flagged as unreachable and
left out of the plan with that reason; Live alerts suggest a re-plan when an approved route uses a
closed street.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

import numpy as np

from . import audit, config, db, fmt, osm


_STREETS: dict[str, dict] = {}


def street_index() -> dict[str, dict]:
    """Street name -> {"lat", "lon" (middle point), "segments", "nodes", "line"} (read once per process)."""
    if _STREETS:
        return _STREETS
    roads = osm.load(osm.ROADS_FILE)
    nodes = {e["id"]: (e["lat"], e["lon"]) for e in roads["elements"] if e["type"] == "node"}
    out: dict[str, dict] = {}
    for w in roads["elements"]:
        if w["type"] != "way" or not w.get("tags", {}).get("highway"):
            continue
        name = w["tags"].get("name")
        if not name:
            continue
        rec = out.setdefault(name, {"segments": 0, "nodes": set(), "pts": []})
        rec["segments"] += 1
        for n in w["nodes"]:
            if n in nodes:
                rec["nodes"].add(n)
                rec["pts"].append(nodes[n])
    for rec in out.values():
        pts = np.array(rec.pop("pts"))
        mid = pts[len(pts) // 2] if len(pts) else (np.nan, np.nan)
        rec["lat"], rec["lon"] = float(mid[0]), float(mid[1])
        rec["line"] = pts.tolist()
    _STREETS.update(out)
    return out


def add(conn: sqlite3.Connection, street: str, until_ts: int, reason: str, user: str,
        from_ts: int | None = None) -> int:
    from_ts = from_ts or db.now()
    if until_ts <= from_ts:
        raise ValueError("The closure must end after it starts")
    with db.tx(conn):
        cur = conn.execute("INSERT INTO road_closures(street, reason, from_ts, until_ts, created_by, created_at)"
                           " VALUES (?,?,?,?,?,?)", (street, reason or "", from_ts, until_ts, user, db.now()))
        audit.log(conn, user, "road_closed", {"street": street, "until": fmt.when(until_ts), "reason": reason})
        db.bump_version(conn)
    return int(cur.lastrowid)


def lift(conn: sqlite3.Connection, closure_id: int, user: str) -> None:
    with db.tx(conn):
        r = db.one(conn, "SELECT street FROM road_closures WHERE id=?", (closure_id,))
        conn.execute("UPDATE road_closures SET until_ts=?, lifted_by=? WHERE id=?", (db.now(), user, closure_id))
        audit.log(conn, user, "road_reopened", {"street": r and r["street"]})
        db.bump_version(conn)


def active(conn: sqlite3.Connection, at_ts: int | None = None) -> list[dict]:
    t = at_ts or db.now()
    return db.rows(conn, "SELECT * FROM road_closures WHERE from_ts<=? AND until_ts>? ORDER BY until_ts", (t, t))


def upcoming(conn: sqlite3.Connection, at_ts: int | None = None) -> list[dict]:
    t = at_ts or db.now()
    return db.rows(conn, "SELECT * FROM road_closures WHERE until_ts>? ORDER BY from_ts", (t,))


def closed_names(conn: sqlite3.Connection, at_ts: int | None = None) -> frozenset[str]:
    return frozenset(r["street"] for r in active(conn, at_ts))


def unreachable_bins(conn: sqlite3.Connection, at_ts: int | None = None) -> dict[str, dict]:
    """bin_id -> closure, for bins whose kerb stop touches only closed streets."""
    from . import roads
    cl = {r["street"]: r for r in active(conn, at_ts)}
    if not cl:
        return {}
    full = roads.network_for(conn, closed=frozenset())
    g = full.graph
    out = {}
    for i, sid in enumerate(full.stop_ids):
        if sid in ("DEPOT", "LANDFILL"):
            continue
        u = int(full.stop_node[i])
        names = {g.edge_name.get((u, v), "") for v, _, _ in g.adj[u]} | \
                {g.edge_name.get((v, u), "") for v in _incoming(g, u)}
        names.discard("")
        if names and names <= set(cl):
            out[sid] = cl[sorted(names)[0]]
    return out


def _incoming(g, u: int) -> list[int]:
    if not hasattr(g, "_radj"):
        radj: list[list[int]] = [[] for _ in range(g.n)]
        for a in range(g.n):
            for b, _, _ in g.adj[a]:
                radj[b].append(a)
        g._radj = radj
    return g._radj[u]


def routes_using(plan: dict, closures: list[dict], streets: dict[str, dict] | None = None) -> dict[str, list[str]]:
    """vehicle_id -> closed street names its route geometry passes through."""
    if not closures:
        return {}
    streets = streets or street_index()
    pts = {}
    for c in closures:
        s = streets.get(c["street"])
        if s:
            pts[c["street"]] = {(round(a, 6), round(b, 6)) for a, b in s["line"]}
    out: dict[str, list[str]] = {}
    for r in plan["routes"]:
        geom = {(round(a, 6), round(b, 6)) for a, b in r.get("geometry", [])}
        hit = [name for name, p in pts.items() if len(geom & p) >= 2]
        if hit:
            out[r["vehicle_id"]] = hit
    return out


def until_text(ts: int) -> str:
    d = datetime.fromtimestamp(ts, config.IST)
    return f"{fmt.hhmm(ts)}" if d.date() == datetime.now(config.IST).date() else fmt.when(ts)
