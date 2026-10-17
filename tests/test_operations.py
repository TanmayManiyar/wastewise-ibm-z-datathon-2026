import json
import threading
import urllib.error
import urllib.request

import pytest

from wastewise import api, auth, complaints, db, live, planner, progress
from conftest import NOW_TS


# ---------------------------------------------------------------- complaint understanding
@pytest.fixture()
def index(conn):
    return complaints.PlaceIndex(db.bins(conn))


@pytest.mark.parametrize("text,bin_id,problem,lang", [
    ("Garbage bin near D-Mart Juinagar is overflowing, please clear it", "D-Mart Juinagar", "overflowing", "en"),
    ("डीमार्ट जुईनगर के पास कूड़ेदान भर गया है", "D-Mart Juinagar", "full", "hi"),
    ("Ryan Internatonal School ke paas kachra road pe aa raha hai", "Ryan International School - Sanpada",
     "overflowing", "mixed"),                                                   # typo tolerated
    ("dead dog lying near Jubilee Church bin", "Jubilee Church", "dead_animal", "en"),
    ("जैपुरिया स्कूल जवळ कचरा पडला आहे", "Jaipuriar School", "overflowing", "mr"),  # transliteration
    ("Bin at Millenium Towers not collected", "Millenium Towers", "not_collected", "en"),
])
def test_understands_place_problem_language(index, text, bin_id, problem, lang):
    u = complaints.understand(text, index)
    assert u.decision == "one"
    assert u.candidates[0].landmark == bin_id
    assert u.problems[0] == problem
    assert u.language == lang


def test_bin_number_and_fire_are_urgent(index):
    u = complaints.understand("बिन नंबर 14 में आग लगी है", index)
    assert u.candidates[0].bin_id == "SNP-014" and u.urgent and u.problems == ["fire"]


@pytest.mark.parametrize("text", ["school ke paas dustbin full hai", "garbage near the hospital",
                                  "सानपाडा स्टेशन जवळ कचराकुंडी भरून वाहत आहे"])
def test_asks_when_several_places_match(index, text):
    u = complaints.understand(text, index)
    assert u.decision == "choose" and len(u.candidates) >= 2


def test_unknown_place(index):
    u = complaints.understand("the bin is overflowing", index)
    assert u.decision == "none" and u.problems == ["overflowing"]


def test_confirm_move_and_resolve(conn, index):
    text = "D-Mart Juinagar dustbin overflowing"
    u = complaints.understand(text, index)
    cid = complaints.record(conn, text, None, u, "api:test", status="new")
    assert db.one(conn, "SELECT status FROM complaints WHERE id=?", (cid,))["status"] == "new"
    complaints.confirm(conn, cid, "SNP-040", "operator")
    r = db.one(conn, "SELECT * FROM readings WHERE source='complaint' ORDER BY id DESC LIMIT 1")
    assert r["bin_id"] == "SNP-040" and r["fill_pct"] == 100
    complaints.move(conn, cid, "SNP-039", "supervisor")
    complaints.move(conn, cid, "SNP-041", "supervisor")
    c = db.one(conn, "SELECT * FROM complaints WHERE id=?", (cid,))
    assert c["bin_id"] == "SNP-041" and c["original_bin_id"] == "SNP-040"   # original kept
    complaints.resolve(conn, cid, "supervisor")
    assert db.one(conn, "SELECT status FROM complaints WHERE id=?", (cid,))["status"] == "resolved"


# ---------------------------------------------------------------- progress and crew
@pytest.fixture()
def approved(conn):
    p = planner.generate(conn, "operator", now_ts=NOW_TS - 3600, seconds=2, seed=4)
    pid = planner.save(conn, p, "operator")
    planner.approve(conn, pid, "supervisor")
    return planner.get(conn, pid)


def test_progress_statuses(conn, approved):
    r = next(x for x in approved["routes"] if len([s for s in x["stops"] if s["kind"] == "bin"]) >= 3)
    b = [s for s in r["stops"] if s["kind"] == "bin"]
    progress.record_crew(conn, approved, r["vehicle_id"], "collected", "crew", b[0]["bin_id"], 75,
                         ts=b[0]["arrive_ts"] + 120)
    progress.record_crew(conn, approved, r["vehicle_id"], "collected", "crew", b[1]["bin_id"], 100,
                         ts=b[1]["arrive_ts"] + 45 * 60)
    progress.record_crew(conn, approved, r["vehicle_id"], "could_not_reach", "crew", b[2]["bin_id"], None,
                         ts=b[2]["arrive_ts"])
    rows = {x["bin_id"]: x for x in progress.status_rows(conn, approved["plan_date"], b[2]["arrive_ts"] + 60)}
    assert rows[b[0]["bin_id"]]["status"] == "collected"
    assert rows[b[1]["bin_id"]]["status"] == "late"
    assert rows[b[2]["bin_id"]]["status"] == "could_not_reach" and rows[b[2]["bin_id"]]["action"]
    end = approved["shift_end_ts"]
    after = progress.status_rows(conn, approved["plan_date"], end + 60)
    assert any(x["status"] == "missed" for x in after)
    csv = progress.to_csv(after, str)
    assert csv.splitlines()[0].startswith("vehicle,bin")


def test_crew_next_stop_and_landfill(conn, approved):
    r = next(x for x in approved["routes"] if any(s["kind"] == "landfill" for s in x["stops"][:-2]))
    states = progress.stop_states(conn, approved, r)
    assert states[0] == "todo"
    bins = {b["id"]: b for b in db.bins(conn)}
    first_lf = next(i for i, s in enumerate(r["stops"]) if s["kind"] == "landfill")
    for s in r["stops"][:first_lf]:
        progress.record_crew(conn, approved, r["vehicle_id"], "collected", "crew", s["bin_id"], 100)
    states = progress.stop_states(conn, approved, r)
    assert states[first_lf] == "todo" and all(x == "done" for x in states[:first_lf])
    assert progress.load_on_board(conn, approved, r, bins) > 0
    progress.record_crew(conn, approved, r["vehicle_id"], "landfill_unload", "crew")
    assert progress.stop_states(conn, approved, r)[first_lf] == "done"
    assert progress.load_on_board(conn, approved, r, bins) == 0
    # the emptied bin now reads as nearly empty
    last = db.one(conn, "SELECT fill_pct, source FROM readings WHERE bin_id=? ORDER BY ts DESC, id DESC LIMIT 1",
                  (r["stops"][0]["bin_id"],))
    assert last["source"] == "crew" and last["fill_pct"] <= 5


def test_crew_taps_before_the_shift_still_count(conn, approved):
    """Rehearsing tomorrow's route today (before the shift window) must still move to the next stop."""
    r = next(x for x in approved["routes"] if x["stops"])
    first = next(s for s in r["stops"] if s["kind"] == "bin")
    early = approved["start_ts"] - 20 * 3600            # the evening before the shift
    progress.record_crew(conn, approved, r["vehicle_id"], "collected", "crew", first["bin_id"], 75, ts=early)
    assert progress.stop_states(conn, approved, r)[r["stops"].index(first)] == "done"
    rows = {x["bin_id"]: x for x in progress.status_rows(conn, approved["plan_date"], early + 60)}
    assert rows[first["bin_id"]]["status"] in ("collected", "late")
    for kind in ("could_not_reach", "bin_missing"):
        nxt = next(s for s, st in zip(r["stops"], progress.stop_states(conn, approved, r))
                   if s["kind"] == "bin" and st == "todo")
        progress.record_crew(conn, approved, r["vehicle_id"], kind, "crew", nxt["bin_id"], ts=early + 60)
        assert progress.stop_states(conn, approved, r)[r["stops"].index(nxt)] == "done"


# ---------------------------------------------------------------- live alerts, demo feed, test events
def test_test_events_show_in_live_alerts(conn):
    desc = live.inject(conn, "hindi_complaint", "SNP-040", "admin")
    assert "complaint" in desc
    a = live.alerts(conn, db.now())
    new = [x for x in a if x["kind"] == "complaint_new"]
    assert new and "SNP-040" in new[0]["candidates"]
    c = db.one(conn, "SELECT * FROM complaints ORDER BY id DESC LIMIT 1")
    assert c["synthetic"] == 1 and c["status"] == "new"


def test_demo_feed_adds_labelled_readings(conn):
    n = live.feed_step(conn, NOW_TS + 600, seed=1)
    assert n > 0
    rows = db.rows(conn, "SELECT * FROM readings WHERE user='demo-feed'")
    assert len(rows) == n and all(r["synthetic"] == 1 for r in rows)


# ---------------------------------------------------------------- REST API
@pytest.fixture()
def server(conn, monkeypatch, tmp_path):
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    from wastewise import config
    monkeypatch.setattr(config, "DB_PATH", __import__("pathlib").Path(path))
    api._local.__dict__.clear()
    key = auth.create_api_key(conn, "test")
    httpd = api.serve("127.0.0.1", 0)
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", key
    httpd.shutdown()


def _call(url, key=None, body=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **({"X-API-Key": key} if key else {})},
                                 method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_api_health_and_auth(server):
    base, key = server
    code, body = _call(base + "/api/health")
    assert code == 200 and body["status"] == "ok" and body["bins"] == 50
    assert _call(base + "/api/events")[0] == 401
    assert _call(base + "/api/events", "wrong")[0] == 401
    assert _call(base + "/api/events", key)[0] == 200


def test_api_readings_validation(server, conn):
    base, key = server
    code, body = _call(base + "/api/readings", key, [{"bin_id": "SNP-001", "fill_pct": 64},
                                                    {"bin_id": "NOPE", "fill_pct": 10},
                                                    {"bin_id": "SNP-002", "fill_pct": "x"}])
    assert code == 207 and body["stored"] == 1 and len(body["errors"]) == 2
    r = db.one(conn, "SELECT * FROM readings WHERE source='api' ORDER BY id DESC LIMIT 1")
    assert r["bin_id"] == "SNP-001" and r["fill_pct"] == 64 and r["user"] == "api:test"


def test_api_complaint_waits_for_confirmation(server, conn):
    base, key = server
    code, body = _call(base + "/api/complaints", key, {"text": "डीमार्ट जुईनगर के पास कूड़ेदान भर गया है"})
    assert code == 200 and body["status"] == "new"
    assert body["understood"]["candidates"][0]["bin_id"] == "SNP-040"
    c = db.one(conn, "SELECT * FROM complaints WHERE id=?", (body["complaint_id"],))
    assert c["bin_id"] is None            # never decided without a person


def test_api_plan_latest(server, approved):
    base, key = server
    code, body = _call(base + f"/api/plan/latest?date={approved['plan_date']}", key)
    assert code == 200 and body["id"] == approved["id"] and body["routes"]
