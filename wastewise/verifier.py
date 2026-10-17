"""Independent plan verifier.

Re-checks a finished plan (the JSON the UI shows and crews follow) using only
the travel matrices, the bin register and the fill forecast - none of the
optimiser's code. A plan that fails is never offered for approval.

Checks: every route starts where the truck is and ends at the depot; times
add up (travel + service/unloading); the truck is never over capacity
between unloads; it unloads at the landfill before returning; it is back
before the end of the shift; no bin is visited twice; only active bins and
trucks are used; every due bin is served or explained; reported totals match.
"""
from __future__ import annotations

import numpy as np

TOL_S = 2.0


def _pickup(cap_l: float, mu_row: np.ndarray, t0: float, step: float, ts: float, max_fill: float) -> float:
    grid = t0 + step * np.arange(len(mu_row))
    f = float(np.interp(ts, grid, mu_row))
    return cap_l * min(max(f, 0.0), max_fill) / 100.0


def verify(plan: dict, ctx: dict) -> dict:
    """ctx: T, D (matrices), stop_index {stop_id: i}, bins {id: row}, vehicles {id: row},
    service_s {bin_id: s}, unload_s, mu {bin_id: array}, risk_t0, risk_step, max_fill_pct,
    usable_capacity, due (set of bin ids), shift_end_ts {vehicle_id: ts}, start {vehicle_id: (stop_id, ts, load)}."""
    T, D, ix = ctx["T"], ctx["D"], ctx["stop_index"]
    errors: list[str] = []
    warnings: list[str] = []
    seen: dict[str, str] = {}
    total_m = 0.0
    total_s = 0.0
    for route in plan["routes"]:
        vid = route["vehicle_id"]
        v = ctx["vehicles"].get(vid)
        if v is None or not v.get("active", 1):
            errors.append(f"{vid}: vehicle is not active")
            continue
        cap = v["capacity_l"] * ctx["usable_capacity"]
        start_stop, t, load = ctx["start"][vid]
        pos = ix[start_stop]
        stops = route["stops"]
        if not stops:
            continue
        route_m = 0.0
        collected_since_unload = False
        for s in stops:
            here = ix[s["stop_id"]]
            expect_arrive = t + T[pos, here]
            if abs(s["arrive_ts"] - expect_arrive) > TOL_S:
                errors.append(f"{vid}: arrival at {s['stop_id']} is {s['arrive_ts'] - expect_arrive:+.0f} s off")
            route_m += D[pos, here]
            total_s += T[pos, here]
            t = s["arrive_ts"]
            if s["kind"] == "bin":
                bid = s["stop_id"]
                b = ctx["bins"].get(bid)
                if b is None or not b["active"]:
                    errors.append(f"{vid}: {bid} is not an active bin")
                    continue
                if bid in seen:
                    errors.append(f"{bid} visited twice ({seen[bid]} and {vid})")
                seen[bid] = vid
                load += _pickup(b["capacity_l"], ctx["mu"][bid], ctx["risk_t0"], ctx["risk_step"], t,
                                ctx["max_fill_pct"])
                if load > cap * (1 + 1e-6):
                    errors.append(f"{vid}: over capacity at {bid} ({load:.0f} L > {cap:.0f} L)")
                collected_since_unload = True
                t += ctx["service_s"][bid]
            elif s["kind"] == "landfill":
                t += ctx["unload_s"]
                if not collected_since_unload and load <= 0:
                    warnings.append(f"{vid}: landfill visit with an empty truck")
                load = 0.0
                collected_since_unload = False
            elif s["kind"] == "depot":
                if load > 1e-6:
                    errors.append(f"{vid}: returns to the depot with {load:.0f} L on board (no final unload)")
            if abs(s["depart_ts"] - t) > TOL_S:
                errors.append(f"{vid}: departure from {s['stop_id']} is {s['depart_ts'] - t:+.0f} s off")
            pos = here
        if stops[-1]["kind"] != "depot" and pos != ix["DEPOT"]:
            errors.append(f"{vid}: route does not end at the depot")
        if t > ctx["shift_end_ts"][vid] + TOL_S:
            errors.append(f"{vid}: back at {(t - ctx['shift_end_ts'][vid]) / 60:.0f} min after the end of the shift")
        rep = route.get("totals", {}).get("distance_km")
        if rep is not None and abs(rep - route_m / 1000) > max(0.05, 0.005 * route_m / 1000):
            errors.append(f"{vid}: reported distance {rep} km but recomputed {route_m / 1000:.2f} km")
        total_m += route_m
    unserved = {u["bin_id"]: u for u in plan.get("unserved", [])}
    for bid in ctx["due"]:
        if bid not in seen and bid not in unserved:
            errors.append(f"{bid} is due but neither scheduled nor listed as left out")
    for bid, u in unserved.items():
        if bid in seen:
            errors.append(f"{bid} listed as left out but is scheduled")
        if u.get("due") and not u.get("reason"):
            errors.append(f"{bid} due bin left out without a reason")
    return {"ok": not errors, "errors": errors, "warnings": warnings,
            "bins_checked": len(seen), "routes_checked": len(plan["routes"]),
            "distance_km": round(float(total_m) / 1000, 2), "drive_min": round(float(total_s) / 60, 1)}
