import copy
import itertools
import math
import random

import numpy as np
import pytest

from wastewise import baseline, beats, db, geo, optimiser, planner, risk, verifier
from conftest import NOW_TS


# ---------------------------------------------------------------- small synthetic instances
def small_problem(n_bins=5, n_veh=1, seed=0, shift_s=4 * 3600, cap=3000.0, due=True, bin_cap=1100.0):
    r = np.random.default_rng(seed)
    m = n_bins + 2                                   # 0 depot, 1 landfill, 2.. bins
    xy = r.uniform(0, 3000, (m, 2))
    xy[1] = [6000, 6000]                             # landfill far away
    D = np.sqrt(((xy[:, None] - xy[None]) ** 2).sum(-1)) * r.uniform(1.0, 1.3, (m, m))
    np.fill_diagonal(D, 0)
    T = D / 6.0
    ids = [f"B{i}" for i in range(n_bins)]
    mu0 = r.uniform(30, 105, n_bins)
    rates = np.tile(r.uniform(2, 8, (n_bins, 1)), (1, 30))
    rc = risk.build(ids, mu0, np.full(n_bins, 4.0), rates, 0, 25 * 60)
    vehicles = [optimiser.Vehicle(f"V{k}", cap, 0, 0.0, float(shift_s)) for k in range(n_veh)]
    return optimiser.Problem(T=T, D=D, depot=0, landfill=1, bin_stop=list(range(2, m)), bin_ids=ids,
                             due=[due] * n_bins, weight=[2.0] * n_bins, service_s=[240.0] * n_bins,
                             bin_capacity_l=[bin_cap] * n_bins, owner=[-1] * n_bins, risk=rc,
                             vehicles=vehicles, unload_s=1200.0)


def brute_force(prob):
    """Exact optimum: every assignment of bins to trucks (or left out) and every order."""
    nb, nv = prob.nb, len(prob.vehicles)
    best = math.inf
    for assign in itertools.product(range(nv + 1), repeat=nb):   # nv = left out
        groups = [[b for b in range(nb) if assign[b] == k] for k in range(nv)]
        left = sum(prob.unserved_cost[b] for b in range(nb) if assign[b] == nv)
        total = left
        for k, g in enumerate(groups):
            total += min(prob.route_cost(k, list(p)) for p in itertools.permutations(g)) if g else \
                prob.route_cost(k, [])
            if total >= best:
                break
        best = min(best, total)
    return best


@pytest.mark.parametrize("seed", range(6))
def test_matches_brute_force_one_truck(seed):
    prob = small_problem(n_bins=6, n_veh=1, seed=seed)
    sol, _ = optimiser.solve(prob, seconds=3, max_iter=400, seed=seed)
    assert sol.total == pytest.approx(brute_force(prob), rel=1e-9)


@pytest.mark.parametrize("seed", range(4))
def test_matches_brute_force_two_trucks(seed):
    prob = small_problem(n_bins=5, n_veh=2, seed=10 + seed, cap=2500.0)
    sol, _ = optimiser.solve(prob, seconds=3, max_iter=400, seed=seed)
    assert sol.total == pytest.approx(brute_force(prob), rel=1e-9)


def test_landfill_trip_when_next_bin_does_not_fit():
    prob = small_problem(n_bins=6, n_veh=1, seed=3, cap=2500.0, shift_s=10 * 3600)
    sol, _ = optimiser.solve(prob, seconds=2, max_iter=200)
    sim = prob.simulate(0, sol.routes[0])
    kinds = [s["kind"] for s in sim["stops"]]
    assert kinds[-2:] == ["landfill", "depot"]          # final unload before the depot
    assert kinds.count("landfill") >= 2                 # > 2500 L of waste needs a mid-route unload
    cap = prob.vehicles[0].capacity_l * prob.params.usable_capacity
    for s in sim["stops"]:
        assert s["load_after_l"] <= cap + 1e-6
        if s["kind"] == "landfill":
            assert s["load_after_l"] == 0


def test_due_bin_left_out_only_when_no_time():
    prob = small_problem(n_bins=6, n_veh=1, seed=1, shift_s=45 * 60)   # far too short a shift
    sol, _ = optimiser.solve(prob, seconds=2, max_iter=300)
    assert sol.unassigned                                    # some due bins cannot fit
    sim = prob.simulate(0, sol.routes[0])
    assert sim["end_ts"] <= prob.vehicles[0].end_ts         # never overtime instead
    # each left-out bin truly does not fit anywhere
    for b in sol.unassigned:
        assert all(prob.route_cost(0, sol.routes[0][:p] + [b] + sol.routes[0][p:]) >= prob.params.overtime_fixed
                   for p in range(len(sol.routes[0]) + 1))


def test_optional_bins_need_a_reason_to_be_served():
    prob = small_problem(n_bins=5, n_veh=1, seed=2, due=False)
    prob.risk.cum_overflow_min[:] = 0.0                      # nothing will overflow
    prob.__post_init__()
    sol, _ = optimiser.solve(prob, seconds=1, max_iter=100)
    assert sum(len(r) for r in sol.routes) == 0              # driving costs more than it saves


def test_beat_penalty_keeps_trucks_in_their_area():
    prob = small_problem(n_bins=6, n_veh=2, seed=5, cap=6000.0)
    prob.owner = [0, 0, 0, 1, 1, 1]
    prob.params.beat_penalty = 500.0
    prob.__post_init__()
    sol, _ = optimiser.solve(prob, seconds=2, max_iter=300)
    for k, r in enumerate(sol.routes):
        assert all(prob.owner[b] == k for b in r)


def test_better_than_nearest_bin_routine():
    prob = small_problem(n_bins=8, n_veh=2, seed=7, cap=4000.0)
    sol, _ = optimiser.solve(prob, seconds=3, max_iter=600)
    base_routes, base_left = baseline.nearest_bin_routes(prob)
    base_cost = optimiser.Solution(prob, base_routes, base_left).total
    assert sol.total <= base_cost + 1e-6


# ---------------------------------------------------------------- beats
def test_beats_are_contiguous_sectors_sized_by_capacity(conn):
    bins = db.bins(conn)
    depot = db.places(conn)["depot"]
    vehicles = db.vehicles(conn)
    daily = {b["id"]: float(b["capacity_l"]) for b in bins}
    bm = beats.compute(bins, daily, depot, vehicles)
    assert set(bm) == {b["id"] for b in bins}
    ang = {b["id"]: geo.bearing_deg(depot["lat"], depot["lon"], b["lat"], b["lon"]) for b in bins}
    order = sorted(bm, key=lambda x: ang[x])
    labels = [bm[x] for x in order]
    changes = sum(1 for a, b in zip(labels, labels[1:] + labels[:1]) if a != b)
    assert changes == len(vehicles)                    # each beat is one angular sector
    vol = {v["id"]: sum(daily[b] for b in bm if bm[b] == v["id"]) for v in vehicles}
    total = sum(vol.values())
    for v in vehicles:
        share = v["capacity_l"] / sum(x["capacity_l"] for x in vehicles)
        assert abs(vol[v["id"]] / total - share) < 0.12


# ---------------------------------------------------------------- full planner on the real ward
@pytest.fixture()
def plan(conn):
    return planner.generate(conn, "tester", now_ts=NOW_TS - 3600, seconds=4, seed=3)


def test_real_plan_feasible_and_verified(plan, conn):
    assert plan["verifier"]["ok"], plan["verifier"]["errors"]
    assert plan["kpis"]["due_left_out"] == 0
    served = {s["bin_id"] for r in plan["routes"] for s in r["stops"] if s["kind"] == "bin"}
    due = {b for b, d in planner.due_status(conn, db.bins(conn), planner.plan_day(conn, NOW_TS - 3600),
                                            NOW_TS - 3600).items() if d["due"]}
    assert due <= served
    for r in plan["routes"]:
        if r["stops"]:
            assert [s["kind"] for s in r["stops"][-2:]] == ["landfill", "depot"]
            assert r["totals"]["end_ts"] <= r["shift_end_ts"]


def test_real_plan_respects_beats_mostly(plan):
    bins = sum(r["totals"]["bins"] for r in plan["routes"])
    off = sum(r["totals"]["off_beat_bins"] for r in plan["routes"])
    assert off <= 0.2 * bins


def test_real_plan_beats_baseline(plan):
    k, b = plan["kpis"], plan["baseline"]
    assert k["bins_served"] >= b["bins_served"]
    assert k["expected_overflow_min"] <= b["expected_overflow_min"]
    assert k["distance_km"] <= b["distance_km"] * 1.05


def test_verifier_catches_broken_plans(plan, conn):
    ctx = planner.build_context(conn, NOW_TS - 3600)
    vctx = planner.verify_context(ctx)
    ok = verifier.verify(plan, vctx)
    assert ok["ok"], ok["errors"]

    bad = copy.deepcopy(plan)       # drop the final landfill unload
    r = next(r for r in bad["routes"] if r["stops"])
    lf = max(i for i, s in enumerate(r["stops"]) if s["kind"] == "landfill")
    del r["stops"][lf]
    assert not verifier.verify(bad, vctx)["ok"]

    bad = copy.deepcopy(plan)       # silently drop a due bin
    r = next(r for r in bad["routes"] if r["stops"])
    i = next(i for i, s in enumerate(r["stops"]) if s["kind"] == "bin")
    del r["stops"][i]
    errs = verifier.verify(bad, vctx)["errors"]
    assert any("due but neither scheduled" in e for e in errs)

    bad = copy.deepcopy(plan)       # visit a bin twice
    r = next(r for r in bad["routes"] if r["stops"])
    s = next(s for s in r["stops"] if s["kind"] == "bin")
    other = next(x for x in bad["routes"] if x is not r and x["stops"])
    other["stops"].insert(0, copy.deepcopy(s))
    assert any("twice" in e for e in verifier.verify(bad, vctx)["errors"])


def test_save_and_approve(plan, conn):
    pid = planner.save(conn, plan, "operator")
    assert planner.current(conn, plan["plan_date"])["status"] == "draft"
    planner.approve(conn, pid, "supervisor")
    p2 = planner.save(conn, plan, "operator")
    planner.approve(conn, p2, "supervisor")
    assert planner.get(conn, pid)["status"] == "superseded"
    assert planner.current(conn, plan["plan_date"])["id"] == p2


def test_daily_plan_refused_during_shift(conn):
    s = db.get_settings(conn)
    start, _ = planner.shift_window(s, planner.plan_day(conn, NOW_TS))
    with pytest.raises(planner.PlanError):
        planner.generate(conn, "x", now_ts=start + 600, seconds=1)


def test_replan_starts_from_progress(plan, conn):
    pid = planner.save(conn, plan, "operator")
    planner.approve(conn, pid, "supervisor")
    p = planner.get(conn, pid)
    r = next(r for r in p["routes"] if r["stops"])
    first = next(s for s in r["stops"] if s["kind"] == "bin")
    db.add_event(conn, "collected", first["bin_id"], r["vehicle_id"], pid, fill_before=90,
                 ts=first["arrive_ts"] + 60, user="crew")
    now = first["arrive_ts"] + 120
    re = planner.generate(conn, "operator", kind="replan", now_ts=now, seconds=3, replan_of=p)
    assert re["verifier"]["ok"], re["verifier"]["errors"]
    assert first["bin_id"] in re["excluded"]
    served = {s["bin_id"] for x in re["routes"] for s in x["stops"] if s["kind"] == "bin"}
    assert first["bin_id"] not in served
    rr = next(x for x in re["routes"] if x["vehicle_id"] == r["vehicle_id"])
    assert rr["start_stop"] == first["bin_id"] and rr["start_load_l"] > 0


def test_breakdown_scenario_moves_bins(conn):
    p = planner.generate(conn, "x", now_ts=NOW_TS - 3600, seconds=3, scenario={"breakdown": ["TRK-2"]})
    assert p["verifier"]["ok"]
    assert {r["vehicle_id"] for r in p["routes"]} == {"TRK-1", "TRK-3"}
