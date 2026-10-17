"""Road closures, landfill gate hours and queue, truck log, crew performance and the monthly summary."""
from collections import Counter

import pytest

from conftest import NOW_TS
from wastewise import closures, db, fleet, live, pdfs, planner, reports, roads


def _street_of_a_bin(conn):
    """A street that is the only street at some bin's kerb stop."""
    net = roads.network_for(conn, closed=frozenset())
    g = net.graph
    for i, sid in enumerate(net.stop_ids):
        if not sid.startswith("SNP-"):
            continue
        u = int(net.stop_node[i])
        names = {g.edge_name.get((u, v), "") for v, _, _ in g.adj[u]}
        if len(names) == 1 and next(iter(names)) in closures.street_index():
            return sid, next(iter(names))
    pytest.skip("no bin sits on a single named street")


def test_closed_road_changes_the_network_and_flags_bins(conn):
    bin_id, street = _street_of_a_bin(conn)
    full = roads.network_for(conn, closed=frozenset())
    cid = closures.add(conn, street, NOW_TS + 86400, "road works", "supervisor", from_ts=NOW_TS - 60)
    assert closures.closed_names(conn, NOW_TS) == {street}
    assert bin_id in closures.unreachable_bins(conn, NOW_TS)
    closed = roads.network_for(conn, at_ts=NOW_TS)
    assert closed is not full and closed.graph.n_edges() < full.graph.n_edges()
    assert all(n != street for n in closed.graph.edge_name.values())
    p = planner.generate(conn, "op", now_ts=NOW_TS, seconds=2.0)
    assert bin_id in p["excluded"] and "road closed" in p["excluded"][bin_id]
    assert all(s.get("bin_id") != bin_id for r in p["routes"] for s in r["stops"])
    assert any("cannot be reached" in a["text"] for a in p["alerts"])
    assert p["verifier"]["ok"]
    closures.lift(conn, cid, "supervisor")
    assert closures.closed_names(conn) == frozenset()


def test_live_alert_when_approved_route_meets_a_new_closure(conn):
    bin_id, street = _street_of_a_bin(conn)
    p = planner.generate(conn, "op", now_ts=NOW_TS, seconds=2.0)
    pid = planner.save(conn, p, "op")
    planner.approve(conn, pid, "supervisor")
    if bin_id not in {s.get("bin_id") for r in p["routes"] for s in r["stops"]}:
        pytest.skip("bin not in the plan")
    closures.add(conn, street, NOW_TS + 86400, "", "supervisor", from_ts=NOW_TS - 60)
    al = [a for a in live.alerts(conn, NOW_TS + 60) if a["kind"] == "closure"]
    assert al and street in al[0]["text"] and "Re-plan" in al[0]["action"]


def test_landfill_gate_hours_and_queue(conn):
    ctx = planner.build_context(conn, NOW_TS, "daily", None, set())
    prob = ctx.prob
    seq = list(range(min(6, prob.nb)))
    k = 0
    base = prob.simulate(k, seq)
    lf = [s for s in base["stops"] if s["kind"] == "landfill"]
    assert lf
    # gate opens 1 h after the truck would arrive: it waits, and the verifier accepts the wait
    prob.lf_open_ts = lf[0]["arrive_ts"] + 3600
    sim = prob.simulate(k, seq)
    first = [s for s in sim["stops"] if s["kind"] == "landfill"][0]
    assert first["wait_s"] == pytest.approx(3600) and first["depart_ts"] == pytest.approx(first["arrive_ts"] + 3600 + prob.unload_s)
    assert prob.route_cost(k, seq) == prob.route_cost_reference(k, seq)
    # gate closed before the truck arrives: as costly as overtime
    prob.lf_open_ts, prob.lf_close_ts = None, lf[0]["arrive_ts"] - 600
    assert prob.route_cost(k, seq) >= base["cost"] + prob.params.overtime_fixed
    s = db.get_settings(conn)
    assert s["landfill_queue_min"] == 10.0
    places = db.places(conn)
    assert planner.build_context(conn, NOW_TS, "daily", None, set()).prob.unload_s == \
        pytest.approx(60 * (places["landfill"]["unload_min"] + 10.0))


def test_plans_respect_the_gate_and_the_verifier_checks_it(conn):
    with db.tx(conn):
        db.set_setting(conn, "landfill_open", "08:00")
    p = planner.generate(conn, "op", now_ts=NOW_TS, seconds=2.0)
    o, _ = planner.landfill_hours(db.get_settings(conn), planner.plan_day(conn, NOW_TS))
    assert p["verifier"]["ok"]
    for r in p["routes"]:
        for st in r["stops"]:
            if st["kind"] == "landfill":
                assert st["depart_ts"] >= o
    bad = {**p, "routes": [dict(r) for r in p["routes"]]}
    ctx = planner.build_context(conn, NOW_TS, "daily", None, set())
    vctx = planner.verify_context(ctx)
    vctx["lf_close_ts"] = o - 3600          # pretend the gate closed before anyone could unload
    from wastewise import verifier
    res = verifier.verify(bad, vctx)
    if any(s["kind"] == "landfill" for r in p["routes"] for s in r["stops"]):
        assert not res["ok"] and any("gate closes" in e for e in res["errors"])


def test_truck_out_of_service_is_left_out(conn):
    fleet.add(conn, "TRK-2", "breakdown", "admin", note="gearbox", out_from=NOW_TS - 3600, out_until=NOW_TS + 3 * 86400)
    assert "TRK-2" in fleet.out_of_service(conn, NOW_TS)
    p = planner.generate(conn, "op", now_ts=NOW_TS, seconds=2.0)
    assert {r["vehicle_id"] for r in p["routes"]} == {"TRK-1", "TRK-3"}
    assert any("Truck 2 is not planned" in a["text"] for a in p["alerts"])
    fleet.add(conn, "TRK-2", "repair_done", "admin", ts=NOW_TS)
    assert "TRK-2" not in fleet.out_of_service(conn, NOW_TS + 60)
    fleet.add(conn, "TRK-1", "fuel", "admin", litres=80, cost_rs=7400, ts=NOW_TS)
    tot = fleet.month_totals(conn, NOW_TS - 86400, NOW_TS + 86400)
    assert tot["TRK-1"]["fuel_l"] == 80 and tot["TRK-2"]["breakdowns"] == 1


def test_crew_performance_and_monthly_summary(conn):
    perf = {r["vehicle_id"]: r for r in reports.crew_performance(conn, NOW_TS)}
    assert set(perf) == {"TRK-1", "TRK-2", "TRK-3"}
    for r in perf.values():
        assert r["visits"] > 50 and 50 <= r["on_time_pct"] <= 100 and 0 <= r["missed_pct"] < 20
    kinds = Counter(e["kind"] for e in db.rows(conn, "SELECT kind FROM events WHERE planned_ts IS NOT NULL"))
    assert kinds["collected"] > 0
    m = reports.monthly_summary(conn, 2026, 10)
    assert m["totals"]["collections"] > 100 and m["synthetic"] and m["days"]
    csv_text = reports.monthly_csv(m)
    assert csv_text.splitlines()[0].startswith("date,collections")
    pdf = pdfs.monthly_report(m)
    assert pdf[:4] == b"%PDF" and len(pdf) > 2000

