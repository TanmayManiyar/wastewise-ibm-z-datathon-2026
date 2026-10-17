"""From the database to a verified, explained plan.

generate():  decide which bins are due / worth collecting, estimate how full
             each bin is and how fast it fills, build overflow-risk curves,
             run the ALNS optimiser, compare with the nearest-bin routine,
             verify independently and write alerts with concrete actions.
save() / approve() store plans as Draft / Approved (one approved plan per day).
Re-planning starts each truck from where it is now, with what it has on board.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import numpy as np

from . import audit, baseline, beats as beats_mod, config, db, geo, optimiser, risk, roads, state, verifier
from .synth import plan_day_for

HORIZON_MIN = 24 * 60 + 60


class PlanError(Exception):
    pass


def shift_window(settings: dict, day: date) -> tuple[int, int]:
    hh, mm = map(int, settings["shift_start"].split(":"))
    start = datetime(day.year, day.month, day.day, hh, mm, tzinfo=config.IST)
    return int(start.timestamp()), int((start + timedelta(hours=float(settings["shift_hours"]))).timestamp())


def plan_day(conn: sqlite3.Connection, now_ts: int | None = None) -> date:
    s = db.get_settings(conn)
    return plan_day_for(datetime.fromtimestamp(now_ts or db.now(), config.IST), s["shift_start"], s["shift_hours"])


def in_shift(conn: sqlite3.Connection, now_ts: int | None = None) -> bool:
    now_ts = now_ts or db.now()
    s = db.get_settings(conn)
    start, end = shift_window(s, datetime.fromtimestamp(now_ts, config.IST).date())
    return start <= now_ts < end


def due_status(conn: sqlite3.Connection, bins: list[dict], day: date, now_ts: int) -> dict[str, dict]:
    out = {}
    for b in bins:
        last = conn.execute("SELECT MAX(ts) FROM events WHERE bin_id=? AND kind='collected' AND ts<=?",
                            (b["id"], now_ts)).fetchone()[0]
        if last is None:
            out[b["id"]] = {"due": True, "days_since": None, "last_ts": None}
            continue
        last_day = datetime.fromtimestamp(last, config.IST).date()
        days = (day - last_day).days
        out[b["id"]] = {"due": days >= int(b["collect_every_days"]), "days_since": days, "last_ts": last}
    return out


def _rates(conn, at_ts: int, hours: int, bins: list[dict], bad_sensors: set[str]):
    """Fill state now and hourly fill rates ahead. Uses the promoted ML forecaster when
    available, otherwise the learned hourly profile."""
    profiles = state.rate_profiles(conn, at_ts, bad_sensors=bad_sensors)
    st = state.estimate(conn, at_ts, profiles, bad_sensors=bad_sensors)
    rates = {b["id"]: state.profile_rates(profiles[b["id"]], at_ts, hours) for b in bins}
    source = "hourly profile (baseline)"
    try:
        from . import forecast
        ml = forecast.predict_rates(conn, at_ts, hours, bins, st)
        if ml is not None:
            rates, source = ml["rates"], ml["source"]
    except ImportError:
        pass
    return st, rates, profiles, source


@dataclass
class Context:
    prob: optimiser.Problem
    net: roads.Network
    bins: dict[str, dict]
    vehicles: list[dict]
    settings: dict
    start_ts: int
    shift_end_ts: int
    day: date
    kind: str
    due: dict[str, dict]
    states: dict
    forecast_source: str
    scenario: dict
    excluded: dict[str, str]


def build_context(conn: sqlite3.Connection, now_ts: int | None = None, kind: str = "daily",
                  scenario: dict | None = None, bad_sensors: set[str] | None = None,
                  plan_for_replan: dict | None = None) -> Context:
    now_ts = now_ts or db.now()
    scenario = scenario or {}
    settings = db.get_settings(conn)
    bins_all = db.bins(conn)
    bins = {b["id"]: b for b in bins_all}
    vehicles = [v for v in db.vehicles(conn) if v["id"] not in set(scenario.get("breakdown", []))]
    if scenario.get("extra_truck"):
        x = scenario["extra_truck"]
        vehicles.append({"id": x.get("id", "TRK-X"), "name": x.get("name", "Spare truck"),
                         "capacity_l": int(x.get("capacity_l", 6000)), "color": x.get("color", "#1E3A8A"),
                         "shift_start": settings["shift_start"], "shift_hours": settings["shift_hours"], "active": 1})
    if not vehicles:
        raise PlanError("No truck is available")
    net = roads.network_for(conn)
    places = db.places(conn)

    day = plan_day(conn, now_ts) if kind != "replan" else datetime.fromtimestamp(now_ts, config.IST).date()
    shift_start, shift_end = shift_window(settings, day)
    if kind == "replan":
        if not (shift_start <= now_ts < shift_end):
            raise PlanError("Re-planning is only possible during shift hours "
                            f"({settings['shift_start']} for {settings['shift_hours']} h)")
        start_ts = now_ts
    else:
        if shift_start <= now_ts < shift_end:
            raise PlanError("The shift is already under way - use 'Re-plan from now' instead")
        start_ts = shift_start

    # Bins already handled today are left out of a re-plan.
    excluded: dict[str, str] = {}
    if kind == "replan":
        for r in conn.execute("SELECT bin_id, kind FROM events WHERE ts BETWEEN ? AND ? AND bin_id IS NOT NULL"
                              " ORDER BY ts", (shift_start, now_ts)):
            if r[1] == "collected":
                excluded[r[0]] = "already collected today"
            elif r[1] == "bin_missing":
                excluded[r[0]] = "reported missing by the crew"

    hours = HORIZON_MIN // 60 + 1
    st, rates, profiles, src = _rates(conn, start_ts, hours, bins_all, bad_sensors or set())
    if scenario.get("surge"):
        sg = scenario["surge"]
        c = bins.get(sg.get("center_bin")) if sg.get("center_bin") else None
        clat, clon = (c["lat"], c["lon"]) if c else (sg["lat"], sg["lon"])
        for bid, b in bins.items():
            if geo.haversine_m(clat, clon, b["lat"], b["lon"]) <= float(sg.get("radius_m", 400)):
                rates[bid] = rates[bid] * float(sg.get("factor", 1.8))
    due = due_status(conn, bins_all, day, now_ts)

    # Candidates: due bins must be served; others only if they will be fairly full.
    ids_all = sorted(bins)
    mu0 = np.array([st[b].fill for b in ids_all])
    sd0 = np.array([st[b].sd for b in ids_all])
    R = np.array([rates[b] for b in ids_all])
    curves_all = risk.build(ids_all, mu0, sd0, R, start_ts, HORIZON_MIN)
    end_k = int((shift_end - start_ts) / curves_all.step_s)
    cand = []
    for i, bid in enumerate(ids_all):
        if bid in excluded:
            continue
        if due[bid]["due"]:
            cand.append(i)
        elif curves_all.mu[i, min(end_k, curves_all.mu.shape[1] - 1)] >= settings["collect_threshold_pct"] \
                or curves_all.p_full[i, min(end_k + 12 * 12, curves_all.mu.shape[1] - 1)] >= 0.5:
            cand.append(i)
    cand_ids = [ids_all[i] for i in cand]
    curves = risk.RiskCurves(cand_ids, start_ts, curves_all.step_s, curves_all.mu[cand], curves_all.sd[cand],
                             curves_all.p_full[cand], curves_all.cum_overflow_min[cand],
                             [curves_all.overflowing_since[i] for i in cand])

    # Beats (recomputed when bins or fleet changed).
    bmap = beats_mod.load(conn)
    if set(bmap) != set(bins) or not set(bmap.values()) <= {v["id"] for v in db.vehicles(conn, active_only=False)}:
        daily_l = {bid: float(profiles[bid].sum() / 7 * bins[bid]["capacity_l"] / 100) for bid in bins}
        idx = {s: i for i, s in enumerate(net.stop_ids)}
        bmap = beats_mod.compute(bins_all, daily_l, places["depot"], db.vehicles(conn), net.time_s, idx)
        beats_mod.save(conn, bmap, "system")
    vindex = {v["id"]: k for k, v in enumerate(vehicles)}
    owner = [vindex.get(bmap.get(bid, ""), -1) for bid in cand_ids]

    # Truck start states.
    dep = net.index("DEPOT")
    vlist = []
    starts = (replan_starts(conn, plan_for_replan, net, now_ts, bins) if kind == "replan" and plan_for_replan else {})
    for v in vehicles:
        pos, load = starts.get(v["id"], (dep, 0.0))
        vlist.append(optimiser.Vehicle(v["id"], float(v["capacity_l"]), pos, float(start_ts), float(shift_end),
                                       float(load), v["color"]))
    s = settings
    service = [60.0 * (s["service_min_base"] + s["service_min_per_1000l"] * bins[b]["capacity_l"] / 1000)
               for b in cand_ids]
    prob = optimiser.Problem(
        T=net.time_s, D=net.dist_m, depot=dep, landfill=net.index("LANDFILL"),
        bin_stop=[net.index(b) for b in cand_ids], bin_ids=cand_ids,
        due=[due[b]["due"] for b in cand_ids],
        weight=[optimiser.PRIORITY_WEIGHT[int(bins[b]["priority"])] for b in cand_ids],
        service_s=service, bin_capacity_l=[float(bins[b]["capacity_l"]) for b in cand_ids],
        owner=owner, risk=curves, vehicles=vlist, unload_s=60.0 * float(places["landfill"]["unload_min"]),
        params=optimiser.CostParams(beat_penalty=float(s["beat_penalty_min"])))
    return Context(prob, net, bins, vehicles, settings, start_ts, shift_end, day, kind, due, st, src, scenario,
                   excluded)


def replan_starts(conn, plan: dict, net: roads.Network, now_ts: int, bins: dict) -> dict[str, tuple[int, float]]:
    """Where each truck is now and how much it carries, from crew updates (or the plan timetable)."""
    out = {}
    for route in plan["routes"]:
        vid = route["vehicle_id"]
        evs = db.rows(conn, "SELECT * FROM events WHERE plan_id=? AND vehicle_id=? AND ts<=? ORDER BY ts",
                      (plan["id"], vid, now_ts))
        pos, load = net.index("DEPOT"), 0.0
        if evs:
            for e in evs:
                if e["kind"] == "landfill_unload":
                    pos, load = net.index("LANDFILL"), 0.0
                elif e["bin_id"] and e["kind"] in ("collected", "could_not_reach", "bin_missing"):
                    pos = net.index(e["bin_id"])
                    if e["kind"] == "collected":
                        f = e["fill_before"] if e["fill_before"] is not None else 80.0
                        load += bins[e["bin_id"]]["capacity_l"] * min(f, 120) / 100
        else:
            # No updates from the crew: assume the truck kept to the timetable.
            for s in route["stops"]:
                if s["depart_ts"] <= now_ts:
                    pos = net.index(s["stop_id"])
                    load = s["load_after_l"]
        out[vid] = (pos, load)
    return out


# ------------------------------------------------------------------ outputs
def kpis(prob: optimiser.Problem, routes: list[list[int]], unassigned: list[int], settings: dict) -> dict:
    sims = [prob.simulate(k, r) for k, r in enumerate(routes)]
    km = sum(s["dist_m"] for s in sims) / 1000
    fuel = km * settings["fuel_l_per_km"]
    over = 0.0
    full = 0
    for s in sims:
        for st in s["stops"]:
            if st["kind"] == "bin":
                over += st["overflow_min"]
                full += st["fill_pct"] >= 100
    K = prob.risk.cum_overflow_min.shape[1] - 1
    left_over = sum(float(prob.risk.cum_overflow_min[b, K]) for b in unassigned)
    return {
        "bins_served": sum(len(r) for r in routes),
        "due_left_out": sum(1 for b in unassigned if prob.due[b]),
        "litres": round(sum(st.get("pickup_l", 0) for s in sims for st in s["stops"])),
        "distance_km": round(km, 2),
        "drive_min": round(sum(s["drive_s"] for s in sims) / 60, 1),
        "fuel_l": round(fuel, 2),
        "co2_kg": round(fuel * settings["co2_kg_per_l_diesel"], 1),
        "expected_overflow_min": round(over, 0),
        "expected_overflow_min_left_out": round(left_over, 0),
        "bins_full_on_arrival": int(full),
        "landfill_trips": sum(s["landfill_trips"] for s in sims),
        "finish_ts": int(max((s["end_ts"] for s in sims if routes[s["vehicle"]]), default=prob.vehicles[0].start_ts)),
    }


def _route_json(ctx: Context, k: int, seq: list[int]) -> dict:
    prob, net = ctx.prob, ctx.net
    sim = prob.simulate(k, seq)
    v = prob.vehicles[k]
    vrow = next(x for x in ctx.vehicles if x["id"] == v.id)
    stops = []
    prev = v.start_pos
    geom: list[list[float]] = []
    for n, s in enumerate(sim["stops"], 1):
        sid = net.stop_ids[s["stop"]]
        rec = {"seq": n, "kind": s["kind"], "stop_id": sid,
               "arrive_ts": int(round(s["arrive_ts"])), "depart_ts": int(round(s["depart_ts"])),
               "load_before_l": round(s["load_before_l"]), "load_after_l": round(s["load_after_l"]),
               "drive_min": round(s["drive_s"] / 60, 1), "drive_km": round(s["drive_m"] / 1000, 2),
               "lat": float(net.graph.lat[s["stop"]]), "lon": float(net.graph.lon[s["stop"]])}
        if s["kind"] == "bin":
            b = ctx.bins[sid]
            i = s["bin"]
            rec.update({"bin_id": sid, "landmark": b["landmark"], "street": b["street"], "site_type": b["site_type"],
                        "priority": b["priority"], "capacity_l": b["capacity_l"],
                        "lat": b["lat"], "lon": b["lon"],
                        "expected_fill_pct": round(s["fill_pct"], 1),
                        "p_full": round(prob.risk.p_full_at(i, s["arrive_ts"]), 3),
                        "status": prob.risk.status_at(i, s["arrive_ts"]),
                        "overflow_min": round(s["overflow_min"], 1), "pickup_l": round(s["pickup_l"]),
                        "own_beat": bool(s["own_beat"]), "due": bool(prob.due[i])})
        stops.append(rec)
        pts = net.path(prev, s["stop"])
        geom.extend([[round(a, 6), round(b_, 6)] for a, b_ in pts])
        prev = s["stop"]
    route = {"vehicle_id": v.id, "vehicle_name": vrow["name"], "color": v.color, "capacity_l": v.capacity_l,
             "start_stop": net.stop_ids[v.start_pos], "start_ts": int(v.start_ts), "start_load_l": round(v.start_load),
             "shift_end_ts": int(v.end_ts), "stops": stops, "geometry": geom,
             "totals": {"bins": len(seq), "litres": round(sum(s.get("pickup_l", 0) for s in sim["stops"])),
                        "distance_km": round(sim["dist_m"] / 1000, 2), "drive_min": round(sim["drive_s"] / 60, 1),
                        "duration_min": round((sim["end_ts"] - v.start_ts) / 60, 1) if seq or v.start_load else 0.0,
                        "landfill_trips": sim["landfill_trips"], "end_ts": int(round(sim["end_ts"])),
                        "off_beat_bins": sum(1 for s in stops if s["kind"] == "bin" and not s["own_beat"])}}
    return route


def _hhmm(ts: float) -> str:
    return datetime.fromtimestamp(ts, config.IST).strftime("%H:%M")


def make_alerts(ctx: Context, plan: dict) -> list[dict]:
    prob = ctx.prob
    out = []
    if not plan["verifier"]["ok"]:
        out.append({"level": "critical", "text": "The plan failed the independent check.",
                    "action": "Do not approve. Generate the plan again and report the problem.",
                    "details": plan["verifier"]["errors"][:5]})
    left = [u for u in plan["unserved"] if u["due"]]
    if left:
        litres = sum(u["expected_l"] for u in left)
        worst = max(left, key=lambda u: u["expected_overflow_min"])
        out.append({"level": "critical",
                    "text": f"{len(left)} due bin(s) do not fit: no truck has space or time left "
                            f"(about {litres:,.0f} L).",
                    "action": f"Send an extra trip or the spare vehicle. Most urgent: {worst['bin_id']} "
                              f"{worst['landmark']}.",
                    "bins": [u["bin_id"] for u in left]})
    red = [s for r in plan["routes"] for s in r["stops"] if s["kind"] == "bin" and s["status"] == "red"]
    if red:
        out.append({"level": "warning", "text": f"{len(red)} bin(s) are expected to be full already at the start.",
                    "action": "Check with the supervisor; these are placed early in their routes.",
                    "bins": [s["bin_id"] for s in red]})
    waits = sorted((s for r in plan["routes"] for s in r["stops"] if s["kind"] == "bin" and s["overflow_min"] >= 30),
                   key=lambda s: -s["overflow_min"])
    if waits:
        s = waits[0]
        vname = next(r["vehicle_name"] for r in plan["routes"] if s in r["stops"])
        out.append({"level": "warning",
                    "text": f"Longest wait: {s['bin_id']} {s['landmark']} is expected to overflow for about "
                            f"{s['overflow_min']:.0f} min before {vname} arrives at {_hhmm(s['arrive_ts'])}.",
                    "action": "If people complain, send the spare vehicle or ask a crew to come earlier.",
                    "bins": [w["bin_id"] for w in waits[:5]]})
    off = sum(r["totals"]["off_beat_bins"] for r in plan["routes"])
    if off:
        out.append({"level": "info", "text": f"{off} bin(s) are served by a truck from another area today.",
                    "action": "No action - this saves time or overflow today."})
    if plan["kpis"]["finish_ts"] > ctx.shift_end_ts - 15 * 60 and plan["kpis"]["bins_served"]:
        out.append({"level": "info", "text": "The busiest truck finishes less than 15 minutes before the end of the shift.",
                    "action": "Avoid delays; a breakdown today would leave bins uncollected."})
    if ctx.excluded:
        out.append({"level": "info", "text": f"{len(ctx.excluded)} bin(s) already handled today are not in this plan.",
                    "action": "None.", "bins": sorted(ctx.excluded)})
    return out


def verify_context(ctx: Context) -> dict:
    prob, net = ctx.prob, ctx.net
    rc = prob.risk
    return {
        "T": net.time_s, "D": net.dist_m, "stop_index": {s: i for i, s in enumerate(net.stop_ids)},
        "bins": ctx.bins, "vehicles": {v["id"]: v for v in ctx.vehicles},
        "service_s": dict(zip(prob.bin_ids, prob.service_s)), "unload_s": prob.unload_s,
        "mu": {b: rc.mu[i] for i, b in enumerate(rc.bin_ids)}, "risk_t0": rc.start_ts, "risk_step": rc.step_s,
        "max_fill_pct": prob.params.max_fill_pct, "usable_capacity": prob.params.usable_capacity,
        "due": {b for b, d in zip(prob.bin_ids, prob.due) if d},
        "shift_end_ts": {v.id: v.end_ts for v in prob.vehicles},
        "start": {v.id: (net.stop_ids[v.start_pos], v.start_ts, v.start_load) for v in prob.vehicles},
    }


def generate(conn: sqlite3.Connection, user: str | None = None, kind: str = "daily", now_ts: int | None = None,
             scenario: dict | None = None, seconds: float | None = None, max_iter: int = 100_000, seed: int = 1,
             bad_sensors: set[str] | None = None, replan_of: dict | None = None) -> dict:
    now_ts = now_ts or db.now()
    if bad_sensors is None:
        try:
            from . import anomaly
            bad_sensors = anomaly.faulty_sensors(conn, now_ts)
        except ImportError:
            bad_sensors = set()
    ctx = build_context(conn, now_ts, kind if kind != "whatif" else "daily", scenario, bad_sensors, replan_of)
    settings = ctx.settings
    seconds = float(settings["optimiser_seconds"] if seconds is None else seconds)
    sol, stats = optimiser.solve(ctx.prob, seconds=seconds, max_iter=max_iter, seed=seed)
    prob = ctx.prob

    routes = [_route_json(ctx, k, r) for k, r in enumerate(sol.routes)]
    unserved = []
    for b in sol.unassigned:
        bid = prob.bin_ids[b]
        K = prob.risk.cum_overflow_min.shape[1] - 1
        unserved.append({"bin_id": bid, "landmark": ctx.bins[bid]["landmark"], "due": bool(prob.due[b]),
                         "reason": "no truck has space or time left" if prob.due[b] else "not needed today",
                         "expected_l": round(prob.load_at(b, ctx.shift_end_ts)),
                         "expected_overflow_min": round(float(prob.risk.cum_overflow_min[b, K]))})
    base_routes, base_left = baseline.nearest_bin_routes(prob)
    base = kpis(prob, base_routes, base_left, settings)
    k_plan = kpis(prob, sol.routes, sol.unassigned, settings)
    plan = {
        "plan_date": ctx.day.isoformat(), "kind": kind, "start_ts": ctx.start_ts, "shift_end_ts": ctx.shift_end_ts,
        "created_at": now_ts, "created_by": user, "label": "computed from SYNTHETIC demonstration data"
        if db.get_meta(conn, "synthetic_info") else None,
        "scenario": ctx.scenario, "forecast_source": ctx.forecast_source,
        "candidates": len(prob.bin_ids), "due_bins": int(sum(prob.due)), "excluded": ctx.excluded,
        "bad_sensors": sorted(bad_sensors or []),
        "routes": routes, "unserved": unserved, "kpis": k_plan, "baseline": base,
        "baseline_routes": [[prob.bin_ids[b] for b in r] for r in base_routes],
        "solver": {"iterations": stats.iterations, "seconds": round(stats.seconds, 2),
                   "initial_cost": round(stats.initial_cost, 1), "best_cost": round(stats.best_cost, 1),
                   "improvements": stats.improvements, "operator_weights": stats.operator_weights, "seed": seed},
        "data_marks": {"data_version": db.data_version(conn),
                       "max_reading_id": conn.execute("SELECT COALESCE(MAX(id),0) FROM readings").fetchone()[0],
                       "max_complaint_id": conn.execute("SELECT COALESCE(MAX(id),0) FROM complaints").fetchone()[0]},
        "fill_at_start": {b: {"fill": round(ctx.states[b].fill, 1), "sd": round(ctx.states[b].sd, 1),
                              "basis": ctx.states[b].basis} for b in ctx.bins},
    }
    plan["verifier"] = verifier.verify(plan, verify_context(ctx))
    plan["alerts"] = make_alerts(ctx, plan)
    return plan


# ------------------------------------------------------------------ storage
def save(conn: sqlite3.Connection, plan: dict, user: str | None, status: str = "draft") -> int:
    with db.tx(conn):
        cur = conn.execute("INSERT INTO plans(created_at, plan_date, status, kind, created_by, start_ts, data_version,"
                           " body) VALUES (?,?,?,?,?,?,?,?)",
                           (plan["created_at"], plan["plan_date"], status, plan["kind"], user, plan["start_ts"],
                            plan["data_marks"]["data_version"], json.dumps(plan)))
        pid = int(cur.lastrowid)
        audit.log(conn, user, "plan_generated", {"plan_id": pid, "kind": plan["kind"], "date": plan["plan_date"],
                                                 "bins": plan["kpis"]["bins_served"],
                                                 "verified": plan["verifier"]["ok"]})
        db.bump_version(conn)
    return pid


def approve(conn: sqlite3.Connection, plan_id: int, user: str) -> None:
    p = get(conn, plan_id)
    if p is None:
        raise PlanError("Plan not found")
    if not p["verifier"]["ok"]:
        raise PlanError("This plan failed the independent check and cannot be approved")
    with db.tx(conn):
        conn.execute("UPDATE plans SET status='superseded' WHERE plan_date=? AND status='approved' AND id<>?",
                     (p["plan_date"], plan_id))
        conn.execute("UPDATE plans SET status='approved', approved_by=?, approved_at=? WHERE id=?",
                     (user, db.now(), plan_id))
        audit.log(conn, user, "plan_approved", {"plan_id": plan_id, "date": p["plan_date"]})
        db.bump_version(conn)


def get(conn: sqlite3.Connection, plan_id: int) -> dict | None:
    r = db.one(conn, "SELECT * FROM plans WHERE id=?", (plan_id,))
    if not r:
        return None
    body = json.loads(r["body"])
    body.update({"id": r["id"], "status": r["status"], "approved_by": r["approved_by"],
                 "approved_at": r["approved_at"], "created_by": r["created_by"]})
    return body


def current(conn: sqlite3.Connection, day: date | str) -> dict | None:
    """The approved plan for a day, else the newest draft."""
    d = day if isinstance(day, str) else day.isoformat()
    r = db.one(conn, "SELECT id FROM plans WHERE plan_date=? AND status='approved' AND kind<>'whatif'"
                     " ORDER BY id DESC LIMIT 1", (d,))
    if not r:
        r = db.one(conn, "SELECT id FROM plans WHERE plan_date=? AND status='draft' AND kind<>'whatif'"
                         " ORDER BY id DESC LIMIT 1", (d,))
    return get(conn, r["id"]) if r else None
