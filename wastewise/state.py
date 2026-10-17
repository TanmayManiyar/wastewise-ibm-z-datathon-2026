"""Current fill estimate of every bin and its typical filling rate.

The estimate starts from the latest trustworthy observation after the last
collection (sensor, supervisor, crew, complaint) and adds the expected filling
since then. Filling rates come from each bin's own history:
  * sensor bins: hourly increases between consecutive readings (ignoring
    collections and saturated readings), by hour of day x day of week;
  * bins without sensors: total fill between collections (crew estimates and
    supervisor readings), shaped by the hourly pattern of sensor bins of the
    same site type.
This is also the "seasonal profile" baseline the ML forecaster must beat.
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime

import numpy as np

from . import config, db

# Typical error (fill %) of each kind of observation.
OBS_SD = {"sensor": 3.0, "api": 3.0, "supervisor": 8.0, "crew": 12.0, "complaint": 6.0}
RESIDUAL_PCT = 2.0
HIST_DAYS = 28


@dataclass
class BinState:
    bin_id: str
    fill: float          # expected fill % now (can exceed 100 = overflowing)
    sd: float
    basis: str           # what the estimate is based on
    basis_ts: int | None
    collected_ts: int | None


def _local(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, config.IST)


def profile_rates(profile: np.ndarray, start_ts: int, hours: int) -> np.ndarray:
    """Expand a (7, 24) %/hour profile into an hourly series from start_ts."""
    d0 = _local(start_ts)
    out = np.empty(hours)
    dow, h = d0.weekday(), d0.hour
    for k in range(hours):
        out[k] = profile[dow, h]
        h += 1
        if h == 24:
            h, dow = 0, (dow + 1) % 7
    return out


def growth_between(profile: np.ndarray, t0: int, t1: int) -> float:
    """Expected fill increase (%) between two times, with fractional hours."""
    if t1 <= t0:
        return 0.0
    total, t = 0.0, t0
    while t < t1:
        d = _local(t)
        next_hour = t + (3600 - (d.minute * 60 + d.second))
        seg = min(next_hour, t1) - t
        total += profile[d.weekday(), d.hour] * seg / 3600
        t += seg
    return total


def _collections(conn: sqlite3.Connection, since: int, until: int) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for r in conn.execute("SELECT bin_id, ts FROM events WHERE kind='collected' AND ts BETWEEN ? AND ?"
                          " ORDER BY ts", (since, until)):
        out.setdefault(r[0], []).append(r[1])
    return out


def rate_profiles(conn: sqlite3.Connection, at_ts: int, days: int = HIST_DAYS,
                  bad_sensors: set[str] | None = None) -> dict[str, np.ndarray]:
    bad_sensors = bad_sensors or set()
    since = at_ts - days * 86400
    bins = db.bins(conn)
    colls = _collections(conn, since, at_ts)
    profiles: dict[str, np.ndarray] = {}
    shapes: dict[str, list[np.ndarray]] = {}

    for b in bins:
        if not b["has_sensor"] or b["id"] in bad_sensors:
            continue
        rs = conn.execute("SELECT ts, fill_pct FROM readings WHERE bin_id=? AND source IN ('sensor','api')"
                          " AND ts BETWEEN ? AND ? AND fill_pct IS NOT NULL ORDER BY ts",
                          (b["id"], since, at_ts)).fetchall()
        if len(rs) < 48:
            continue
        ts = np.array([r[0] for r in rs], dtype=np.int64)
        v = np.array([r[1] for r in rs], dtype=float)
        dt = np.diff(ts) / 3600
        dv = np.diff(v)
        ok = (dt > 0.4) & (dt < 1.6) & (v[:-1] < 97) & (v[1:] < 97) & (dv > -3) & (dv < 60)
        rate = np.where(ok, dv / np.maximum(dt, 1e-6), np.nan)
        loc = [_local(int(t)) for t in ts[:-1]]
        hod = np.array([d.hour for d in loc])
        dow = np.array([d.weekday() for d in loc])
        hod_mean = np.array([np.nanmean(rate[hod == h]) if np.any(~np.isnan(rate[hod == h])) else np.nan
                             for h in range(24)])
        overall = np.nanmean(rate)
        if not np.isfinite(overall) or overall <= 0:
            continue
        hod_mean = np.where(np.isfinite(hod_mean), hod_mean, overall)
        dow_f = np.array([np.nanmean(rate[dow == d]) / overall if np.any(~np.isnan(rate[dow == d])) else 1.0
                          for d in range(7)])
        dow_f = np.clip(np.nan_to_num(dow_f, nan=1.0), 0.05, 3.0)
        prof = np.clip(np.outer(dow_f, hod_mean), 0, None)
        profiles[b["id"]] = prof
        shapes.setdefault(b["site_type"], []).append(prof / max(prof.mean(), 1e-6))

    all_shapes = [s for v in shapes.values() for s in v]
    global_shape = np.mean(all_shapes, axis=0) if all_shapes else np.ones((7, 24))

    for b in bins:
        if b["id"] in profiles:
            continue
        shape = np.mean(shapes[b["site_type"]], axis=0) if b["site_type"] in shapes else global_shape
        # Daily fill from what crews and supervisors saw between collections.
        slopes: list[float] = []
        lower_bounds: list[float] = []
        c = colls.get(b["id"], [])
        for prev, cur in zip(c, c[1:]):
            r = db.one(conn, "SELECT fill_before FROM events WHERE bin_id=? AND ts=? AND kind='collected'",
                       (b["id"], cur))
            if r and r["fill_before"] is not None and cur - prev > 3 * 3600:
                s = (r["fill_before"] - RESIDUAL_PCT) / ((cur - prev) / 3600)
                # "100 % full" is only a lower bound: rank it above every exact value.
                slopes.append(math.inf if r["fill_before"] >= 100 else s)
                lower_bounds.append(s)
        for r in conn.execute("SELECT ts, fill_pct FROM readings WHERE bin_id=? AND source='supervisor'"
                              " AND ts BETWEEN ? AND ?", (b["id"], since, at_ts)):
            prev = max((x for x in c if x < r[0]), default=None)
            if prev and r[0] - prev > 3 * 3600 and r[1] is not None and r[1] < 97:
                slopes.append((r[1] - RESIDUAL_PCT) / ((r[0] - prev) / 3600))
        # Median that tolerates censoring (valid while fewer than half are censored).
        mean_rate = float(np.median(slopes)) if slopes else 3.0   # %/hour
        if not math.isfinite(mean_rate):
            mean_rate = 1.15 * max(lower_bounds)
        profiles[b["id"]] = shape * max(mean_rate, 0.1)
    return profiles


def estimate(conn: sqlite3.Connection, at_ts: int, profiles: dict[str, np.ndarray],
             bad_sensors: set[str] | None = None) -> dict[str, BinState]:
    bad_sensors = bad_sensors or set()
    out = {}
    for b in db.bins(conn):
        bid = b["id"]
        c = conn.execute("SELECT MAX(ts) FROM events WHERE bin_id=? AND kind='collected' AND ts<=?",
                         (bid, at_ts)).fetchone()[0]
        srcs = ("supervisor", "crew", "complaint") if (bid in bad_sensors) else \
            ("sensor", "api", "supervisor", "crew", "complaint")
        q = ("SELECT ts, fill_pct, source FROM readings WHERE bin_id=? AND ts<=? AND ts>=? AND fill_pct IS NOT NULL"
             f" AND source IN ({','.join('?' * len(srcs))}) ORDER BY ts DESC LIMIT 1")
        o = conn.execute(q, (bid, at_ts, c or 0, *srcs)).fetchone()
        prof = profiles[bid]
        if o is not None:
            base, base_ts, sd0, basis = float(o[1]), int(o[0]), OBS_SD.get(o[2], 10.0), o[2]
        elif c is not None:
            base, base_ts, sd0, basis = RESIDUAL_PCT, int(c), 3.0, "collected"
        else:
            base, base_ts, sd0, basis = 50.0, at_ts, 30.0, "unknown"
        g = growth_between(prof, base_ts, at_ts)
        hours = (at_ts - base_ts) / 3600
        sd = math.sqrt(sd0 ** 2 + (0.2 * g) ** 2 + 0.25 * hours)
        fill = base + g
        if o is not None and base >= 97:
            # A saturated reading only says "at least full": project from the last
            # unsaturated reading after the collection, if that is higher.
            u = conn.execute(q.replace("fill_pct IS NOT NULL", "fill_pct < 95"), (bid, at_ts, c or 0, *srcs)).fetchone()
            if u is not None:
                alt = float(u[1]) + growth_between(prof, int(u[0]), at_ts)
                if alt > fill:
                    g2 = alt - float(u[1])
                    fill, basis = alt, f"{o[2]} (saturated; projected)"
                    sd = math.sqrt(OBS_SD.get(u[2], 10.0) ** 2 + (0.2 * g2) ** 2 + 0.25 * (at_ts - u[0]) / 3600)
        out[bid] = BinState(bid, fill, sd, basis, base_ts, c)
    return out
