"""Live alerts for the control room, plus the administrator's demonstration feed
and test events (both clearly labelled synthetic / test)."""
from __future__ import annotations

import json
import random
import sqlite3
import threading
import time
from datetime import datetime

from . import anomaly, audit, complaints, config, db, planner, progress, state


def _hm(ts) -> str:
    return datetime.fromtimestamp(ts, config.IST).strftime("%H:%M") if ts else "-"


def alerts(conn: sqlite3.Connection, now_ts: int | None = None) -> list[dict]:
    now_ts = now_ts or db.now()
    day = planner.plan_day(conn, now_ts).isoformat()
    bins = {b["id"]: b for b in db.bins(conn)}
    out: list[dict] = []
    # Complaints waiting for a place confirmation, and open urgent ones.
    for c in db.rows(conn, "SELECT * FROM complaints WHERE status='new' ORDER BY ts DESC LIMIT 20"):
        p = json.loads(c["parsed"] or "{}")
        cands = ", ".join(f"{x['bin_id']} {x['landmark']}" for x in p.get("candidates", [])[:3]) or "place not recognised"
        out.append({"level": "critical" if p.get("urgent") else "warning", "kind": "complaint_new", "ts": c["ts"],
                    "id": c["id"], "text": f"New complaint ({c['lang']}): “{c['text']}”",
                    "action": f"Confirm the place ({cands}) under Bins > Report a problem or below.",
                    "candidates": [x["bin_id"] for x in p.get("candidates", [])]})
    for c in db.rows(conn, "SELECT * FROM complaints WHERE status='open' AND ts>=? ORDER BY ts DESC LIMIT 20",
                     (now_ts - 86400,)):
        urgent = c["problem"] in complaints.URGENT
        b = bins.get(c["bin_id"], {})
        out.append({"level": "critical" if urgent else "warning", "kind": "complaint", "ts": c["ts"], "id": c["id"],
                    "text": f"Complaint at {c['bin_id']} {b.get('landmark', '')}: "
                            f"{complaints.PROBLEM_WORDS.get(c['problem'], c['problem'])} (since {_hm(c['ts'])})",
                    "action": "Send the nearest crew now - fire or dead animal is a health risk." if urgent else
                              "Bin now shows as full; check its planned visit or Re-plan from now."})
    # Sensors.
    for bid, h in anomaly.sensor_health(conn, now_ts).items():
        if h["status"] != "ok":
            out.append({"level": "warning", "kind": "sensor", "ts": h["last_reading_ts"] or now_ts,
                        "text": f"Sensor at {bid} {bins[bid]['landmark']} is {h['status']}: {', '.join(h['reasons'])}.",
                        "action": "Planning ignores this sensor; ask the supervisor to check the bin and the sensor."})
    for s in anomaly.surges(conn, now_ts):
        out.append({"level": "warning", "kind": "surge", "ts": s["since_ts"],
                    "text": f"Surge at {s['bin_id']} {s['landmark']}: +{s['observed_rise']:.0f} points in the last hours, "
                            f"{s['ratio']}x the normal pattern.",
                    "action": "Possible event or market rush - consider Re-plan from now or the What-if 'rush' scenario."})
    # Crew exceptions on today's plan.
    for r in progress.status_rows(conn, day, now_ts):
        if r["status"] in ("could_not_reach", "bin_missing", "overdue", "late"):
            out.append({"level": "warning" if r["status"] in ("late", "overdue") else "critical", "kind": "progress",
                        "ts": r["actual_ts"] or now_ts,
                        "text": f"{r['vehicle_name']} · {r['bin_id']} {r['landmark']}: {r['status'].replace('_', ' ')}"
                                + (f" ({r['delay_min']} min)" if r["delay_min"] else ""),
                        "action": r["action"]})
    order = {"critical": 0, "warning": 1, "info": 2}
    out.sort(key=lambda a: (order[a["level"]], -(a["ts"] or 0)))
    return out


# ------------------------------------------------------------------ demonstration feed
def feed_step(conn: sqlite3.Connection, now_ts: int | None = None, seed: int | None = None) -> int:
    """Add one synthetic sensor reading per sensor bin for 'now', continuing each bin's pattern."""
    now_ts = now_ts or db.now()
    rng = random.Random(seed if seed is not None else now_ts)
    prof = state.rate_profiles(conn, now_ts)
    n = 0
    with db.tx(conn):
        for b in db.bins(conn):
            if not b["has_sensor"]:
                continue
            last = conn.execute("SELECT ts, fill_pct FROM readings WHERE bin_id=? AND source IN ('sensor','api')"
                                " AND ts<=? ORDER BY ts DESC LIMIT 1", (b["id"], now_ts)).fetchone()
            coll = conn.execute("SELECT MAX(ts) FROM events WHERE bin_id=? AND kind='collected' AND ts<=?",
                                (b["id"], now_ts)).fetchone()[0]
            if last is None:
                continue
            t0, v0 = last
            if coll and coll > t0:
                t0, v0 = coll, 2.0
            v = v0 + state.growth_between(prof[b["id"]], t0, now_ts) + rng.gauss(0, 1.0)
            conn.execute("INSERT INTO readings(bin_id, ts, fill_pct, source, synthetic, user, note)"
                         " VALUES (?,?,?,?,1,?,?)", (b["id"], now_ts, round(min(max(v, 0), 100), 1), "sensor",
                                                    "demo-feed", "live demonstration feed (synthetic)"))
            n += 1
        db.bump_version(conn)
    return n


class FeedThread(threading.Thread):
    """Runs feed_step every `interval` seconds while Settings 'demo_feed' is on."""

    def __init__(self, interval: int = 120):
        super().__init__(daemon=True, name="wastewise-demo-feed")
        self.interval = interval
        self.last_run: int | None = None
        self.last_count = 0

    def run(self) -> None:
        conn = db.connect()
        while True:
            try:
                if db.get_settings(conn).get("demo_feed"):
                    self.last_count = feed_step(conn)
                    self.last_run = db.now()
            except Exception:
                pass
            time.sleep(self.interval)


# ------------------------------------------------------------------ test events
TEST_EVENTS = {
    "hindi_complaint": "Hindi complaint arrives (as from the API)",
    "overflow_reading": "Sensor reports a bin full",
    "stuck_sensor": "A sensor gets stuck",
    "surge": "Sudden rush at a bin (surge)",
}
HINDI = ["{lm} के पास कूड़ेदान भर गया है, कचरा सड़क पर गिर रहा है",
         "{lm} के पास कूड़ा नहीं उठाया, बहुत बदबू आ रही है"]


def inject(conn: sqlite3.Connection, kind: str, bin_id: str, user: str) -> str:
    now = db.now()
    b = next(x for x in db.bins(conn) if x["id"] == bin_id)
    note = "TEST EVENT (synthetic)"
    if kind == "hindi_complaint":
        lm = {"D-Mart Juinagar": "डीमार्ट जुईनगर"}.get(b["landmark"], b["landmark"])
        text = random.choice(HINDI).format(lm=lm)
        und = complaints.understand(text, complaints.PlaceIndex(db.bins(conn)))
        cid = complaints.record(conn, text, None, und, "api:test", reporter="test event", synthetic=True, status="new")
        desc = f"complaint #{cid}: {text}"
    elif kind == "overflow_reading":
        db.add_reading(conn, bin_id, 100.0, "api", ts=now, user="api:test", note=note, synthetic=True)
        desc = f"{bin_id} reported 100 % full"
    elif kind == "stuck_sensor":
        with db.tx(conn):
            for h in range(13):
                db.add_reading(conn, bin_id, 37.0, "api", ts=now - (12 - h) * 3600 + 30, user="api:test", note=note,
                               synthetic=True)
        desc = f"{bin_id} sensor stuck at 37 % for 12 h"
    elif kind == "surge":
        with db.tx(conn):
            for h, f in enumerate([30, 45, 60, 75, 92]):
                db.add_reading(conn, bin_id, f, "api", ts=now - (4 - h) * 3600 + 20, user="api:test", note=note,
                               synthetic=True)
        desc = f"{bin_id} rose from 30 % to 92 % in 4 h"
    else:
        raise ValueError(kind)
    audit.log(conn, user, "test_event_injected", {"kind": kind, "bin_id": bin_id, "detail": desc})
    db.bump_version(conn)
    return desc
