"""Live alerts for the control room, plus the administrator's demonstration feed
and test events (both clearly labelled synthetic / test)."""
from __future__ import annotations

import json
import random
import sqlite3
import threading
import time
from datetime import datetime

from . import anomaly, audit, complaints, config, db, fmt, planner, progress, state


REASON = {"stuck": "it has shown the same value for hours", "spike": "it jumps up and down",
          "jump": "it shows impossible jumps", "no_drop": "it did not drop after the bin was emptied",
          "unusual pattern (IsolationForest)": "its readings look unusual",
          "no reading for 6+ hours": "no reading for over 6 hours"}
STATUS_TEXT = {"could_not_reach": "could not reach the bin", "bin_missing": "bin missing",
               "overdue": "not collected yet, overdue", "late": "collected late"}


def _hm(ts) -> str:
    return fmt.hhmm(ts) if ts else "-"


def alerts(conn: sqlite3.Connection, now_ts: int | None = None) -> list[dict]:
    now_ts = now_ts or db.now()
    day = planner.today(now_ts).isoformat()
    bins = {b["id"]: b for b in db.bins(conn)}
    out: list[dict] = []
    # Complaints waiting for a place confirmation, and open urgent ones.
    for c in db.rows(conn, "SELECT * FROM complaints WHERE status='new' ORDER BY ts DESC LIMIT 20"):
        p = json.loads(c["parsed"] or "{}")
        cands = " or ".join(x["landmark"] for x in p.get("candidates", [])[:3]) or "place not recognised"
        lang = {"en": "English", "hi": "Hindi", "mr": "Marathi", "mixed": "mixed language"}.get(c["lang"], c["lang"])
        out.append({"level": "critical" if p.get("urgent") else "warning", "kind": "complaint_new", "ts": c["ts"],
                    "id": c["id"], "text": f"New complaint in {lang}: “{c['text']}”",
                    "action": f"Probably {cands} - confirm the bin below.",
                    "candidates": [x["bin_id"] for x in p.get("candidates", [])]})
    ready = {c["id"]: c for c in complaints.ready_to_close(conn)}
    for c in ready.values():
        b = bins.get(c["bin_id"], {})
        out.append({"level": "info", "kind": "complaint_ready", "ts": c["followup"]["ts"], "id": c["id"],
                    "text": f"Complaint {complaints.ref(c['id'])} at {b.get('landmark', 'a bin')}: "
                            f"{complaints.followup_text(c['followup'])} - ready to close",
                    "action": "Check that the problem is gone (call the citizen if needed), then press Close. "
                              "WasteWise never closes complaints by itself."})
    for c in db.rows(conn, "SELECT * FROM complaints WHERE status='open' AND ts>=? ORDER BY ts DESC LIMIT 20",
                     (now_ts - 86400,)):
        if c["id"] in ready:
            continue
        urgent = c["problem"] in complaints.URGENT
        b = bins.get(c["bin_id"], {})
        out.append({"level": "critical" if urgent else "warning", "kind": "complaint", "ts": c["ts"], "id": c["id"],
                    "text": f"Complaint {complaints.ref(c['id'])} at {b.get('landmark', 'an unconfirmed place')}: "
                            f"{complaints.PROBLEM_WORDS.get(c['problem'], c['problem'])} (since {_hm(c['ts'])})",
                    "action": "Send the nearest crew now - fire or dead animal is a health risk." if urgent else
                              "Bin now shows as full; check its planned visit or Re-plan from now."})
    # Sensors.
    for bid, h in anomaly.sensor_health(conn, now_ts).items():
        if h["status"] != "ok":
            out.append({"level": "warning", "kind": "sensor", "ts": h["last_reading_ts"] or now_ts,
                        "text": f"The sensor at {bins[bid]['landmark']} is {h['status']}: {', '.join(REASON.get(x, x) for x in h['reasons'])}.",
                        "action": "Planning ignores this sensor; ask the supervisor to check the bin and the sensor."})
    for s in anomaly.surges(conn, now_ts):
        out.append({"level": "warning", "kind": "surge", "ts": s["since_ts"],
                    "text": f"{s['landmark']} is filling unusually fast: it rose by {s['observed_rise']:.0f}% of the bin "
                            f"in the last hours, {s['ratio']:.1f} times faster than normal.",
                    "action": "Possible event or market rush - consider Re-plan from now or the What-if 'rush' scenario."})
    # Crew exceptions on today's plan.
    for r in progress.status_rows(conn, day, now_ts):
        if r["status"] in ("could_not_reach", "bin_missing", "overdue", "late"):
            out.append({"level": "warning" if r["status"] in ("late", "overdue") else "critical", "kind": "progress",
                        "ts": r["actual_ts"] or now_ts,
                        "text": f"{r['vehicle_name']} at {r['landmark']}: {STATUS_TEXT[r['status']]}"
                                + (f" ({r['delay_min']} min)" if r["delay_min"] else ""),
                        "action": r["action"]})
    # Evening round: recommended but not planned yet.
    main = planner.current(conn, day)
    if main and main["status"] == "approved" and planner.current(conn, day, round_="extra") is None:
        from . import evening
        a = evening.assess_cached(conn, main)
        if a["candidates"]:
            out.append({"level": "warning", "kind": "evening", "ts": now_ts,
                        "text": f"{len(a['candidates'])} bin(s) will overflow again before tomorrow's round "
                                f"(first one full at {_hm(a['candidates'][0]['full_at'])}).",
                        "action": f"Plan the evening round under Today's plan - best time {_hm(a['suggested_start_ts'])}."})
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


def feed_on(conn: sqlite3.Connection) -> bool:
    """The live demonstration feed is on by default in demonstration mode ("auto")."""
    v = db.get_settings(conn).get("demo_feed", "auto")
    return bool(db.get_meta(conn, "synthetic_info")) if v == "auto" else bool(v)


ROLL_AFTER_S = 2 * 3600     # roll the synthetic history forward once it is older than this
ROLL_CHECK_S = 3600         # ... checked at start-up and then every hour
FEED_EVERY_S = 120


def roll_forward_if_old(conn: sqlite3.Connection, now_ts: int | None = None) -> dict | None:
    """Demonstration mode: regenerate the synthetic history up to now when it ends more than 2 hours ago,
    so the demo always shows 'today'. Records entered by people or the API are kept."""
    now_ts = now_ts or db.now()
    raw = db.get_meta(conn, "synthetic_info")
    if not raw:
        return None
    info = json.loads(raw)
    if now_ts - int(info.get("end_ts", 0)) <= ROLL_AFTER_S:
        return None
    from . import synth
    new = synth.generate(conn, end_ts=now_ts, days=int(info.get("days", 180)), seed=int(info.get("seed", 2026)))
    audit.log(conn, "system", "synthetic_rolled_forward",
              {"from": _hm(info.get("end_ts")), "to": _hm(new["end_ts"]), "rows": new["rows"]})
    return new


class DemoDaemon(threading.Thread):
    """One per web-app process: rolls the synthetic history forward (start-up, then hourly) and runs the
    live demonstration feed (a synthetic reading per sensor bin every 2 minutes) while it is on."""

    def __init__(self):
        super().__init__(daemon=True, name="wastewise-demo")
        self.last_run: int | None = None
        self.last_count = 0
        self.last_roll: int | None = None
        self.last_check = 0.0
        self.error: str | None = None

    def run(self) -> None:
        conn = db.connect()
        next_feed = 0.0
        try:
            from . import crews
            crews.seed_demo(conn)          # labelled demonstration crew list (only if none exists)
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
        while True:
            try:
                if time.time() - self.last_check >= ROLL_CHECK_S:
                    self.last_check = time.time()
                    if roll_forward_if_old(conn):
                        self.last_roll = db.now()
                if feed_on(conn) and time.time() >= next_feed:
                    next_feed = time.time() + FEED_EVERY_S
                    self.last_count = feed_step(conn)
                    self.last_run = db.now()
                self.error = None
            except Exception as exc:     # keep the thread alive; shown under Demonstration tools
                self.error = f"{type(exc).__name__}: {exc}"
            time.sleep(15)


_DAEMON: DemoDaemon | None = None
_DAEMON_LOCK = threading.Lock()


def demo_daemon() -> DemoDaemon:
    global _DAEMON
    with _DAEMON_LOCK:
        if _DAEMON is None or not _DAEMON.is_alive():
            _DAEMON = DemoDaemon()
            _DAEMON.start()
        return _DAEMON


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
        desc = f"complaint: {text}"
    elif kind == "overflow_reading":
        db.add_reading(conn, bin_id, 100.0, "api", ts=now, user="api:test", note=note, synthetic=True)
        desc = f"{b['landmark']} reported 100% full"
    elif kind == "stuck_sensor":
        with db.tx(conn):
            for h in range(13):
                db.add_reading(conn, bin_id, 37.0, "api", ts=now - (12 - h) * 3600 + 30, user="api:test", note=note,
                               synthetic=True)
        desc = f"the sensor at {b['landmark']} stuck at 37% for 12 hours"
    elif kind == "surge":
        with db.tx(conn):
            for h, f in enumerate([30, 45, 60, 75, 92]):
                db.add_reading(conn, bin_id, f, "api", ts=now - (4 - h) * 3600 + 20, user="api:test", note=note,
                               synthetic=True)
        desc = f"{b['landmark']} rose from 30% to 92% in 4 hours"
    else:
        raise ValueError(kind)
    audit.log(conn, user, "test_event_injected", {"kind": kind, "bin_id": bin_id, "detail": desc})
    db.bump_version(conn)
    return desc
