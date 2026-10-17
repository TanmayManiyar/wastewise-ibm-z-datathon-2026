"""Complaint follow-up (never closed automatically), citizen messages, and who is on each truck."""
import urllib.parse

import pytest

from conftest import NOW_TS
from wastewise import complaints, crews, db, live, planner, progress


def _complaint(conn, bin_id, ts):
    with db.tx(conn):
        cur = conn.execute("INSERT INTO complaints(ts, text, lang, bin_id, problem, status, reporter, created_by)"
                           " VALUES (?,?,?,?,?,?,?,?)", (ts, "bin overflowing", "hi", bin_id, "overflowing", "open",
                                                         "Mrs Rao 98200 12345", "operator"))
    return int(cur.lastrowid)


def test_synthetic_complaints_are_closed_by_a_person_not_by_the_system(conn):
    rows = db.rows(conn, "SELECT * FROM complaints WHERE synthetic=1")
    assert rows
    closed = [r for r in rows if r["status"] == "resolved"]
    assert closed and all(r["resolved_by"] == "supervisor (synthetic)" for r in closed)
    assert all(r["resolved_by"] != "synthetic" for r in rows)


def test_collection_makes_a_complaint_ready_to_close_but_never_closes_it(conn):
    b = db.bins(conn)[0]
    cid = _complaint(conn, b["id"], NOW_TS + 60)
    assert complaints.followup(conn, db.one(conn, "SELECT * FROM complaints WHERE id=?", (cid,))) is None
    with db.tx(conn):
        db.add_event(conn, "collected", b["id"], "TRK-2", None, ts=NOW_TS + 3600, user="crew2", person="Mahesh")
    r = db.one(conn, "SELECT * FROM complaints WHERE id=?", (cid,))
    assert r["status"] == "open"                                  # still open: only a person closes it
    f = complaints.followup(conn, r)
    assert f["vehicle_name"] == "Truck 2" and f["by"] == "Mahesh"
    assert "ready to close" not in complaints.followup_text(f) and "by Truck 2" in complaints.followup_text(f)
    assert cid in [x["id"] for x in complaints.ready_to_close(conn)]
    alerts = live.alerts(conn, now_ts=NOW_TS + 3700)
    assert any(a["kind"] == "complaint_ready" and a["id"] == cid for a in alerts)
    for who in ("system", "synthetic", "api:sensor", ""):
        with pytest.raises(ValueError):
            complaints.resolve(conn, cid, who)
    complaints.resolve(conn, cid, "supervisor")
    r = db.one(conn, "SELECT * FROM complaints WHERE id=?", (cid,))
    assert r["status"] == "resolved" and r["resolved_by"] == "supervisor"
    assert cid not in [x["id"] for x in complaints.ready_to_close(conn)]


def test_reference_numbers_and_whatsapp_messages(conn):
    b = db.bins(conn)[0]
    cid = _complaint(conn, b["id"], NOW_TS)
    r = db.one(conn, "SELECT * FROM complaints WHERE id=?", (cid,))
    assert complaints.ref(7) == "SNP-C-0007"
    for lang in ("en", "hi", "mr"):
        text = complaints.acknowledgement(r, b["landmark"], lang)
        assert complaints.ref(cid) in text and b["landmark"] in text
    assert complaints.message_lang(r) == "hi"
    url = complaints.whatsapp_url("hello", r["reporter"])
    assert url.startswith("https://wa.me/919820012345?")
    assert urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["text"] == ["hello"]
    assert complaints.whatsapp_url("x", "citizen call").startswith("https://wa.me/?")
    with db.tx(conn):
        db.add_event(conn, "collected", b["id"], "TRK-1", None, ts=NOW_TS + 1800, user="crew1")
    complaints.resolve(conn, cid, "operator")
    r = db.one(conn, "SELECT * FROM complaints WHERE id=?", (cid,))
    for lang in ("en", "hi", "mr"):
        text = complaints.resolution(conn, r, b["landmark"], lang)
        assert complaints.ref(cid) in text
    assert "Truck 1" in complaints.resolution(conn, r, b["landmark"], "en")


def test_crew_names_are_stored_with_every_tap(conn):
    assert crews.seed_demo(conn) == 9 and crews.seed_demo(conn) == 0
    names = [m["name"] for m in crews.members(conn) if m["vehicle_id"] == "TRK-1"]
    assert all("(demo)" in n for n in names)
    day = "2026-10-17"
    assert crews.on_truck(conn, day, "TRK-1") is None
    with pytest.raises(ValueError):
        crews.set_on_truck(conn, day, "TRK-1", names[:2], names[2], "crew1")   # holder must be on the truck
    crews.set_on_truck(conn, day, "TRK-1", names[:2], names[1], "crew1")
    cur = crews.on_truck(conn, day, "TRK-1")
    assert cur["members"] == names[:2] and cur["holder"] == names[1]
    assert crews.names_for_day(conn, day)["TRK-1"] == ", ".join(names[:2])
    p = planner.generate(conn, "op", now_ts=NOW_TS, seconds=2.0)
    pid = planner.save(conn, p, "op")
    planner.approve(conn, pid, "supervisor")
    plan = planner.get(conn, pid)
    stop = next(s for r in plan["routes"] if r["vehicle_id"] == "TRK-1" for s in r["stops"] if s["kind"] == "bin")
    progress.record_crew(conn, plan, "TRK-1", "collected", "crew1", stop["bin_id"], 75.0,
                         ts=stop["arrive_ts"], person=names[1])
    e = db.one(conn, "SELECT * FROM events WHERE plan_id=? AND bin_id=?", (pid, stop["bin_id"]))
    assert e["person"] == names[1] and e["user"] == "crew1"
    rows = progress.status_rows(conn, plan["plan_date"], stop["arrive_ts"] + 60)
    assert next(r for r in rows if r["bin_id"] == stop["bin_id"])["by"] == names[1]
    with pytest.raises(ValueError):
        crews.add_member(conn, names[0], "TRK-2", "admin")       # no duplicates


def test_route_sheet_shows_the_crew_names(conn):
    from wastewise import pdfs
    p = planner.generate(conn, "op", now_ts=NOW_TS, seconds=2.0)
    r = next(x for x in p["routes"] if x["stops"])
    pdf = pdfs.route_sheets(p, [r], {r["vehicle_id"]: "Ramesh Patil (demo), Sunil Kamble (demo), Anita Jadhav (demo)"})
    assert pdf[:4] == b"%PDF" and len(pdf) > 1000
    assert pdfs._short("A" * 20 + ", " + "B" * 20 + ", " + "C" * 20).endswith(", ...")
