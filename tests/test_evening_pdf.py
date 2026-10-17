import pytest

from wastewise import db, evening, pdfs, planner, progress
from conftest import NOW_TS


@pytest.fixture()
def approved(conn):
    p = planner.generate(conn, "operator", now_ts=NOW_TS - 3600, seconds=3, seed=4)
    pid = planner.save(conn, p, "operator")
    planner.approve(conn, pid, "supervisor")
    return planner.get(conn, pid)


# ---------------------------------------------------------------- PDF documents
def test_route_sheet_pdf(approved, conn):
    pdf = pdfs.route_sheets(approved, approved["routes"], {"TRK-1": "Crew - Truck 1"}, "Sanpada vehicle depot (demo)")
    assert pdf[:5] == b"%PDF-" and len(pdf) > 3000
    assert b"/URI (https://www.google.com/maps" in pdf          # clickable map links


def test_devanagari_names_printable():
    assert pdfs._latin("मनमोहन रेस्टॉरंट") == "Manmohan Restaurant"
    assert all(ord(ch) < 256 for ch in pdfs._latin("जुई गाव - Juinagar"))


def test_shift_report_pdf(approved, conn):
    rows = progress.status_rows(conn, approved["plan_date"], approved["shift_end_ts"] + 60)
    pdf = pdfs.shift_report(approved["plan_date"], rows, progress.summary(rows), {"missed": "Missed"}, demo=True)
    assert pdf[:5] == b"%PDF-"


# ---------------------------------------------------------------- evening round
def test_assessment_and_evening_plan(approved, conn):
    a = evening.assess(conn, approved, min_overflow_min=0)
    assert a["suggested_start_ts"] is None or a["suggested_start_ts"] >= approved["shift_end_ts"]
    assert a["overflow_min_with"] <= a["overflow_min_without"]
    bins = [x["bin_id"] for x in a["candidates"][:5]] or [db.bins(conn)[0]["id"]]
    start = a["suggested_start_ts"] or approved["shift_end_ts"] + 6 * 3600
    ev = evening.plan_round(conn, "operator", approved, start, start + 3 * 3600, bins,
                            now_ts=approved["start_ts"] - 3600, assessment=a)
    assert ev["kind"] == "extra" and ev["verifier"]["ok"], ev["verifier"]["errors"]
    served = {s["bin_id"] for r in ev["routes"] for s in r["stops"] if s["kind"] == "bin"}
    assert served <= set(bins) and ev["kpis"]["due_left_out"] + len(served) == len(bins)
    for r in ev["routes"]:
        for s in r["stops"]:
            assert start <= s["arrive_ts"] <= start + 3 * 3600 + 1
    # bins emptied in the morning are expected to be fairly empty again by the evening
    morning = {s["bin_id"] for r in approved["routes"] for s in r["stops"] if s["kind"] == "bin"}
    for r in ev["routes"]:
        for s in r["stops"]:
            if s["kind"] == "bin" and s["bin_id"] in morning:
                assert s["expected_fill_pct"] < 150


def test_evening_round_has_its_own_approval_and_progress(approved, conn):
    bins = [s["bin_id"] for s in next(r for r in approved["routes"] if r["stops"])["stops"] if s["kind"] == "bin"][:3]
    start = approved["shift_end_ts"] + 6 * 3600
    ev = evening.plan_round(conn, "operator", approved, start, start + 3 * 3600, bins, now_ts=approved["start_ts"] - 3600)
    eid = planner.save(conn, ev, "operator")
    planner.approve(conn, eid, "supervisor")
    assert planner.current(conn, approved["plan_date"])["id"] == approved["id"]          # morning plan untouched
    assert planner.current(conn, approved["plan_date"], round_="extra")["id"] == eid
    # morning collection of a bin does not tick off its evening visit
    r = next(x for x in approved["routes"] if x["stops"])
    for s in r["stops"]:
        if s["kind"] == "bin":
            progress.record_crew(conn, approved, r["vehicle_id"], "collected", "crew", s["bin_id"], 80,
                                 ts=s["arrive_ts"])
    rows = progress.status_rows(conn, approved["plan_date"], approved["shift_end_ts"] + 60)
    evening_rows = [x for x in rows if x["round"] == "Evening"]
    assert evening_rows and all(x["status"] == "pending" for x in evening_rows)
    # once the morning route is done, the crew phone shows the evening route
    evp = planner.get(conn, eid)
    ev_route = next(x for x in evp["routes"] if x["stops"])
    p, route = progress.route_for(conn, approved["plan_date"], ev_route["vehicle_id"])
    if ev_route["vehicle_id"] == r["vehicle_id"]:
        assert p["id"] == eid


# ---------------------------------------------------------------- best plan without a time slider
def test_search_result_is_consistent_without_a_time_setting(conn):
    """No time slider: the search stops when it stops improving, then restarts once from its best plan.
    Different random starts must land within 1 % of each other."""
    costs = [planner.generate(conn, "x", now_ts=NOW_TS - 3600, seed=s)["solver"]["best_cost"] for s in (1, 2)]
    assert max(costs) <= min(costs) * 1.01
