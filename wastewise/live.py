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
        test = bool(c["synthetic"] and (c["reporter"] or "").startswith("test"))
        out.append({"level": "critical" if p.get("urgent") else "warning", "kind": "complaint_new", "ts": c["ts"],
                    "id": c["id"], "test": test, "text": ("TEST · " if test else "") + f"New complaint in {lang}: “{c['text']}”",
                    "action": f"Probably {cands} - confirm the bin below.",
                    "candidates": [x["bin_id"] for x in p.get("candidates", [])]})
    # Predictions: bins the forecaster expects to overflow before their next collection, merged with
    # any open complaint about the same bin (one alert per bin).
    from . import outlook_now
    pred = outlook_now.predict(conn, now_ts)
    open_c: dict[str, list[dict]] = {}
    for c in db.rows(conn, "SELECT * FROM complaints WHERE status='open' AND bin_id IS NOT NULL ORDER BY ts DESC"):
        if not complaints.followup(conn, c):          # collected since: ready to close (complaint register)
            open_c.setdefault(c["bin_id"], []).append(c)
    shown = set()
    for bid, pr in sorted(pred["bins"].items(), key=lambda kv: (kv[1]["full_at"] or 0) if not kv[1]["full_now"] else -1):
        cs = open_c.get(bid, [])
        over = pr["overflow_min"]
        if over < 30 and not cs:
            continue
        b = pr["bin"]
        urgent = any(c["problem"] in complaints.URGENT for c in cs)
        test = any(c["synthetic"] and (c["reporter"] or "").startswith("test") for c in cs)
        when_full = ("already full" if pr["full_now"] else
                     f"predicted full at {_hm(pr['full_at'])}" if pr["full_at"] else "not predicted to fill up")
        lines = [f"{b['landmark']}: {when_full} ({pr['source']}); {outlook_now.visit_text(pr['next_visit'])}"]
        if over >= 30:
            lines.append(f"expected to overflow for about {fmt.duration(over)} before then "
                         f"({pr['p_full_at_visit'] * 100:.0f}% chance it is full when the truck arrives)")
        for c in cs[:2]:
            lines.append(f"complaint {complaints.ref(c['id'])} at {_hm(c['ts'])}: "
                         f"{complaints.PROBLEM_WORDS.get(c['problem'], c['problem'] or 'other')}")
        out.append({"level": "critical" if urgent or over >= 180 else "warning", "kind": "predicted", "bin_id": bid,
                    "ts": (cs[0]["ts"] if cs else pr["full_at"] or now_ts), "test": test,
                    "text": ("TEST · " if test else "") + " - ".join(lines),
                    "action": ("Fire or dead animal: send the nearest crew now. " if urgent else "")
                              + outlook_now.advice(conn, now_ts, pr["next_visit"])})
        shown.add(bid)
    # Sensors.
    for bid, h in anomaly.sensor_health(conn, now_ts).items():
        if h["status"] != "ok":
            out.append({"level": "warning", "kind": "sensor", "ts": h["last_reading_ts"] or now_ts,
                        "text": f"The sensor at {bins[bid]['landmark']} is {h['status']}: {', '.join(REASON.get(x, x) for x in h['reasons'])}.",
                        "action": "Planning ignores this sensor; ask the supervisor to check the bin and the sensor."})
    for sg in outlook_now.model_surges(conn, now_ts):
        pr = pred["bins"].get(sg["bin_id"], {})
        full = ("it is full now" if pr.get("full_now") else
                f"now predicted full at {_hm(pr['full_at'])}" if pr.get("full_at") else "")
        out.append({"level": "warning", "kind": "surge", "ts": sg["since_ts"], "test": sg["test"],
                    "text": ("TEST · " if sg["test"] else "")
                            + f"{sg['landmark']} is filling faster than the forecaster predicted: it rose "
                              f"{sg['observed']}% of the bin since {_hm(sg['since_ts'])}, the model expected "
                              f"{sg['predicted']:.0f}% ({sg['ratio']:.1f} times more)" + (f"; {full}" if full else "") + ".",
                    "action": "Possible event or market rush - " + outlook_now.advice(conn, now_ts, pr.get("next_visit"))})
    # Crew exceptions on today's plan.
    for r in progress.status_rows(conn, day, now_ts):
        if r["status"] in ("could_not_reach", "bin_missing", "overdue", "late"):
            out.append({"level": "warning" if r["status"] in ("late", "overdue") else "critical", "kind": "progress",
                        "ts": r["actual_ts"] or now_ts,
                        "text": f"{r['vehicle_name']} at {r['landmark']}: {STATUS_TEXT[r['status']]}"
                                + (f" ({r['delay_min']} min)" if r["delay_min"] else ""),
                        "action": r["action"]})
    # Closed roads and trucks out of service that affect today's approved plan.
    from . import closures, fleet
    main_ = planner.current(conn, day)
    if main_ and main_["status"] == "approved":
        cl = closures.active(conn, now_ts)
        blocked = closures.unreachable_bins(conn, now_ts)
        planned = {s["bin_id"] for r in main_["routes"] for s in r["stops"] if s["kind"] == "bin"}
        hit = sorted(planned & set(blocked))
        using = closures.routes_using(main_, [c for c in cl if c["created_at"] > main_["created_at"]])
        if hit or using:
            streets = sorted({blocked[b]["street"] for b in hit} | {s for v in using.values() for s in v})
            names = ", ".join(r["vehicle_name"] for r in main_["routes"] if r["vehicle_id"] in using)
            out.append({"level": "warning", "kind": "closure", "ts": now_ts,
                        "text": f"Road closed: {', '.join(streets)}"
                                + (f" - {len(hit)} planned bin(s) cannot be reached" if hit else "")
                                + (f" - {names} route(s) use it" if names else "") + ".",
                        "action": ("Re-plan from now - the new routes avoid the closed road."
                                   if planner.in_shift(conn, now_ts) else
                                   "Generate the plan again before the shift - the new routes avoid the closed road.")})
        off = fleet.out_of_service(conn, now_ts)
        for r in main_["routes"]:
            if r["vehicle_id"] in off and r["totals"]["bins"]:
                out.append({"level": "critical", "kind": "vehicle", "ts": now_ts,
                            "text": f"{r['vehicle_name']} is out of service ({fleet.reason_text(off[r['vehicle_id']])}) "
                                    "but has a route today.",
                            "action": "Re-plan from now so the other trucks take its bins, or send the spare vehicle."})
    order = {"critical": 0, "warning": 1, "info": 2}
    # real alerts first, then test events; most serious first; predictions by time
    out.sort(key=lambda a: (bool(a.get("test")), order[a["level"]], -(a["ts"] or 0) if a["kind"] != "predicted" else (a["ts"] or 0)))
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
        next_llm_check = 0.0
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
                if time.time() >= next_llm_check:
                    next_llm_check = time.time() + 300
                    from . import llm
                    if llm.selfcheck_needed(conn):       # new or changed model: test it before offering it
                        llm.selfcheck(conn)
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
