"""Vehicle areas (beats): contiguous sectors around the depot sized by truck capacity.

Bins are sorted by bearing from the depot and cut into consecutive angular
sectors whose expected daily waste is proportional to each truck's capacity.
Every possible starting angle is tried; the split with the shortest
within-sector road travel times wins. Crews then work their own part of the
ward instead of criss-crossing it; the optimiser penalises serving another
truck's bin but may still do it when that clearly helps.
"""
from __future__ import annotations

import sqlite3

import numpy as np

from . import db, geo


def compute(bins: list[dict], daily_l: dict[str, float], depot: dict, vehicles: list[dict],
            time_s: np.ndarray | None = None, index: dict[str, int] | None = None) -> dict[str, str]:
    if not bins or not vehicles:
        return {}
    ang = [geo.bearing_deg(depot["lat"], depot["lon"], b["lat"], b["lon"]) for b in bins]
    order = [bins[i] for i in np.argsort(ang)]
    vol = np.array([max(daily_l.get(b["id"], 1.0), 1.0) for b in order])
    caps = np.array([v["capacity_l"] for v in vehicles], dtype=float)
    share = np.cumsum(caps / caps.sum())[:-1]
    n = len(order)
    best = None
    for r in range(n):
        seq = list(range(r, n)) + list(range(0, r))
        cum = np.cumsum(vol[seq]) / vol.sum()
        cuts = [int(np.argmin(np.abs(cum - s))) + 1 for s in share]
        cuts = sorted(set(min(max(c, 1), n - 1) for c in cuts))
        if len(cuts) != len(share):
            continue
        groups = np.split(np.array(seq), cuts)
        score = 0.0
        for g in groups:
            if len(g) < 2:
                continue
            if time_s is not None and index is not None:
                ix = [index[order[i]["id"]] for i in g]
                sub = time_s[np.ix_(ix, ix)]
                score += float(sub.sum()) / len(g)
            else:
                la = np.array([order[i]["lat"] for i in g])
                lo = np.array([order[i]["lon"] for i in g])
                score += float(geo.haversine_matrix(la, lo).sum()) / len(g)
        if best is None or score < best[0]:
            best = (score, groups)
    out = {}
    for v, g in zip(vehicles, best[1]):
        for i in g:
            out[order[i]["id"]] = v["id"]
    return out


def save(conn: sqlite3.Connection, beats: dict[str, str], user: str | None = None) -> None:
    from . import audit
    with db.tx(conn):
        conn.execute("DELETE FROM beats")
        conn.executemany("INSERT INTO beats(bin_id, vehicle_id) VALUES (?, ?)", sorted(beats.items()))
        db.set_meta(conn, "beats_computed_at", str(db.now()))
        audit.log(conn, user, "beats_recomputed", {"bins": len(beats)})


def load(conn: sqlite3.Connection) -> dict[str, str]:
    return {r[0]: r[1] for r in conn.execute("SELECT bin_id, vehicle_id FROM beats")}
