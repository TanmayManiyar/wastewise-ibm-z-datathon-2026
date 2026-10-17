"""The comparison baseline: the nearest-bin routine.

Whichever truck is free first drives to the nearest bin still waiting (by
road travel time), goes to the landfill when the next bin would not fit, and
stops taking bins when it could no longer finish, unload and return before
the end of the shift. This mimics a crew working without a plan. It is
evaluated with exactly the same rules as WasteWise routes, so distance, fuel,
CO2 and overflow figures are comparable.
"""
from __future__ import annotations

from .optimiser import Problem


def nearest_bin_routes(prob: Problem, include_optional: bool = False) -> tuple[list[list[int]], list[int]]:
    pool = [b for b in range(prob.nb) if prob.due[b] or include_optional]
    routes: list[list[int]] = [[] for _ in prob.vehicles]
    pos = [v.start_pos for v in prob.vehicles]
    clock = [v.start_ts for v in prob.vehicles]
    done = [False] * len(prob.vehicles)
    T = prob.Tl
    while pool and not all(done):
        k = min((i for i in range(len(routes)) if not done[i]), key=lambda i: clock[i])
        b = min(pool, key=lambda x: T[pos[k]][prob.bin_stop[x]])
        trial = routes[k] + [b]
        sim = prob.simulate(k, trial)
        if sim["end_ts"] > prob.vehicles[k].end_ts:
            done[k] = True
            continue
        routes[k] = trial
        pool.remove(b)
        # position and clock after serving b (ignoring the final return legs)
        served = [s for s in sim["stops"] if s["kind"] == "bin"][-1]
        pos[k] = served["stop"]
        clock[k] = served["depart_ts"]
    return routes, pool
