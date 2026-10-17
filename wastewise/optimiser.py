"""Route optimiser: Adaptive Large Neighbourhood Search (ALNS), written for WasteWise.

Model
-----
Each truck leaves its start point (the depot, or where it is now when
re-planning), visits an ordered list of bins and returns to the depot.
Landfill trips are not decided by the search: route evaluation walks the
list and, whenever the next bin would not fit in the remaining capacity, the
truck drives to the landfill, unloads (fixed unloading time) and continues.
Every route with waste on board ends with a final unload before the depot.

The amount picked up at a bin depends on when the truck arrives (bins keep
filling), so loads, landfill trips and overflow are evaluated at the actual
arrival times.

Cost (lower is better), all in "minute-equivalents":
  driving minutes + fuel cost per km
  + weight(priority) x expected overflow minutes before each visit
  + penalty for serving a bin in another truck's beat
  + for bins left out: expected overflow until the same time tomorrow,
    plus a large penalty if the bin was due
  + a larger penalty for any minute past the end of the shift.
Leaving a due bin out is therefore only chosen when no truck has the space
or time to take it.

Search
------
ALNS with destroy operators (random, worst, related/Shaw, route segment,
off-beat) and repair operators (greedy, regret-2, noisy greedy), adaptive
operator weights, simulated-annealing acceptance, and a final local search
(relocate, 2-opt, cross-route swap). Pure Python; deterministic for a seed
and iteration budget.
"""
from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field

import numpy as np

from .risk import RiskCurves

PRIORITY_WEIGHT = {1: 3.0, 2: 2.0, 3: 1.0}


@dataclass
class Vehicle:
    id: str
    capacity_l: float
    start_pos: int          # stop index in the matrices
    start_ts: float
    end_ts: float           # latest return to depot
    start_load: float = 0.0
    color: str = "#2563EB"


@dataclass
class CostParams:
    drive_min: float = 1.0          # per minute of driving
    per_km: float = 1.5             # fuel/wear, per km
    overflow: float = 2.0           # per weighted expected overflow minute
    beat_penalty: float = 12.0      # per bin served outside the truck's beat
    due_unserved: float = 10_000.0  # per due bin left out
    overtime_fixed: float = 20_000.0
    overtime_per_min: float = 500.0
    max_fill_pct: float = 120.0     # spill-over a crew also picks up
    usable_capacity: float = 0.95   # keep a margin in the truck


@dataclass
class Problem:
    T: np.ndarray                   # (m, m) seconds between stops
    D: np.ndarray                   # (m, m) metres
    depot: int
    landfill: int
    bin_stop: list[int]             # stop index of each candidate bin
    bin_ids: list[str]
    due: list[bool]
    weight: list[float]
    service_s: list[float]
    bin_capacity_l: list[float]
    owner: list[int]                # vehicle index owning the bin's beat, -1 if none
    risk: RiskCurves                # rows aligned with bin_ids
    vehicles: list[Vehicle]
    unload_s: float
    params: CostParams = field(default_factory=CostParams)
    lf_open_ts: float | None = None     # landfill gate hours: trucks wait for opening,
    lf_close_ts: float | None = None    # and may not start unloading after closing

    def _landfill_start(self, arrive: float) -> tuple[float, bool]:
        """(time unloading starts, gate already closed?)"""
        t = arrive if self.lf_open_ts is None or arrive >= self.lf_open_ts else self.lf_open_ts
        return t, self.lf_close_ts is not None and t > self.lf_close_ts

    def __post_init__(self):
        self.Tl = self.T.tolist()
        self.Dl = self.D.tolist()
        self.nb = len(self.bin_ids)
        rc = self.risk
        self._t0 = float(rc.start_ts)
        self._step = float(rc.step_s)
        self._K = rc.mu.shape[1] - 1
        self._mu = rc.mu.tolist()
        self._over = rc.cum_overflow_min.tolist()
        # unserved cost: expected overflow until the same time tomorrow (or horizon end)
        end_k = self._K
        self.unserved_cost = []
        for b in range(self.nb):
            c = self.weight[b] * rc.cum_overflow_min[b, end_k] * self.params.overflow
            if self.due[b]:
                c += self.params.due_unserved
            self.unserved_cost.append(c)
        # nearest neighbours by travel time, for related removal / granular insertion
        S = np.array(self.bin_stop)
        tt = self.T[np.ix_(S, S)] + self.T[np.ix_(S, S)].T
        self.neighbours = [list(np.argsort(tt[b])[1:13]) for b in range(self.nb)]

    # --------------------------------------------------------------- lookups
    def _interp(self, arr: list[list[float]], b: int, ts: float) -> float:
        k = (ts - self._t0) / self._step
        if k <= 0:
            return arr[b][0]
        if k >= self._K:
            return arr[b][self._K]
        k0 = int(k)
        f = k - k0
        row = arr[b]
        return row[k0] + (row[k0 + 1] - row[k0]) * f

    def load_at(self, b: int, ts: float) -> float:
        f = self._interp(self._mu, b, ts)
        f = min(max(f, 0.0), self.params.max_fill_pct)
        return self.bin_capacity_l[b] * f / 100.0

    def overflow_at(self, b: int, ts: float) -> float:
        return self._interp(self._over, b, ts)

    # --------------------------------------------------------------- evaluation
    def route_cost(self, k: int, seq: list[int]) -> float:
        """Cost of one route. The hot loop of the search: the fill and overflow lookups of load_at() and
        overflow_at() are written out here (same arithmetic, about twice as fast)."""
        v = self.vehicles[k]
        p = self.params
        T, D = self.Tl, self.Dl
        LF, DEP = self.landfill, self.depot
        MU, OV, CAPL, STOP, W = self._mu, self._over, self.bin_capacity_l, self.bin_stop, self.weight
        t0, step, K, maxf = self._t0, self._step, self._K, p.max_fill_pct
        cap = v.capacity_l * p.usable_capacity
        t = v.start_ts
        load = v.start_load
        pos = v.start_pos
        drive = 0.0
        dist = 0.0
        over = 0.0
        beat = 0
        closed_hits = 0
        for b in seq:
            sb = STOP[b]
            ta = t + T[pos][sb]
            kk = (ta - t0) / step
            row = MU[b]
            if kk <= 0:
                f = row[0]
            elif kk >= K:
                f = row[K]
            else:
                k0 = int(kk)
                f = row[k0] + (row[k0 + 1] - row[k0]) * (kk - k0)
            if f < 0.0:
                f = 0.0
            elif f > maxf:
                f = maxf
            lb = CAPL[b] * f / 100.0
            if load + lb > cap and load > 0:
                drive += T[pos][LF]
                dist += D[pos][LF]
                t, shut = self._landfill_start(t + T[pos][LF])
                t += self.unload_s
                closed_hits += shut
                load = 0.0
                pos = LF
                ta = t + T[LF][sb]
                kk = (ta - t0) / step
                if kk <= 0:
                    f = row[0]
                elif kk >= K:
                    f = row[K]
                else:
                    k0 = int(kk)
                    f = row[k0] + (row[k0 + 1] - row[k0]) * (kk - k0)
                if f < 0.0:
                    f = 0.0
                elif f > maxf:
                    f = maxf
                lb = CAPL[b] * f / 100.0
            drive += T[pos][sb]
            dist += D[pos][sb]
            row = OV[b]
            if kk <= 0:
                o = row[0]
            elif kk >= K:
                o = row[K]
            else:
                k0 = int(kk)
                o = row[k0] + (row[k0 + 1] - row[k0]) * (kk - k0)
            over += W[b] * o
            o = self.owner[b]
            if o >= 0 and o != k:
                beat += 1
            t = ta + self.service_s[b]
            load += lb
            pos = sb
        if load > 0:
            drive += T[pos][LF]
            dist += D[pos][LF]
            t, shut = self._landfill_start(t + T[pos][LF])
            t += self.unload_s
            closed_hits += shut
            pos = LF
        if pos != DEP:
            drive += T[pos][DEP]
            dist += D[pos][DEP]
            t += T[pos][DEP]
        cost = p.drive_min * drive / 60.0 + p.per_km * dist / 1000.0 + p.overflow * over + p.beat_penalty * beat
        cost += p.overtime_fixed * closed_hits          # landfill gate closed: as bad as overtime
        late = t - v.end_ts
        if late > 0:
            cost += p.overtime_fixed + p.overtime_per_min * late / 60.0
        return cost

    def route_cost_reference(self, k: int, seq: list[int]) -> float:
        """route_cost written with the lookup helpers (kept for the equivalence test)."""
        v = self.vehicles[k]
        p = self.params
        T, D = self.Tl, self.Dl
        LF, DEP = self.landfill, self.depot
        cap = v.capacity_l * p.usable_capacity
        t, load, pos = v.start_ts, v.start_load, v.start_pos
        drive = dist = over = 0.0
        beat = 0
        closed_hits = 0
        for b in seq:
            sb = self.bin_stop[b]
            ta = t + T[pos][sb]
            lb = self.load_at(b, ta)
            if load + lb > cap and load > 0:
                drive += T[pos][LF]
                dist += D[pos][LF]
                t, shut = self._landfill_start(t + T[pos][LF])
                t += self.unload_s
                closed_hits += shut
                load, pos = 0.0, LF
                ta = t + T[LF][sb]
                lb = self.load_at(b, ta)
            drive += T[pos][sb]
            dist += D[pos][sb]
            over += self.weight[b] * self.overflow_at(b, ta)
            o = self.owner[b]
            if o >= 0 and o != k:
                beat += 1
            t = ta + self.service_s[b]
            load += lb
            pos = sb
        if load > 0:
            drive += T[pos][LF]
            dist += D[pos][LF]
            t, shut = self._landfill_start(t + T[pos][LF])
            t += self.unload_s
            closed_hits += shut
            pos = LF
        if pos != DEP:
            drive += T[pos][DEP]
            dist += D[pos][DEP]
            t += T[pos][DEP]
        cost = p.drive_min * drive / 60.0 + p.per_km * dist / 1000.0 + p.overflow * over + p.beat_penalty * beat
        cost += p.overtime_fixed * closed_hits          # landfill gate closed: as bad as overtime
        late = t - v.end_ts
        if late > 0:
            cost += p.overtime_fixed + p.overtime_per_min * late / 60.0
        return cost

    def simulate(self, k: int, seq: list[int]) -> dict:
        """Detailed timeline of one route (same rules as route_cost)."""
        v = self.vehicles[k]
        p = self.params
        T, D = self.Tl, self.Dl
        LF, DEP = self.landfill, self.depot
        cap = v.capacity_l * p.usable_capacity
        t, load, pos = v.start_ts, v.start_load, v.start_pos
        stops = []
        drive = dist = over = 0.0
        n_lf = 0

        def leg(a, b):
            nonlocal drive, dist
            drive += T[a][b]
            dist += D[a][b]
            return T[a][b], D[a][b]

        def landfill():
            nonlocal t, load, pos, n_lf
            dt, dm = leg(pos, LF)
            arrive = t + dt
            start, shut = self._landfill_start(arrive)
            stops.append({"kind": "landfill", "stop": LF, "arrive_ts": arrive, "depart_ts": start + self.unload_s,
                          "wait_s": start - arrive, "gate_closed": shut,
                          "load_before_l": load, "load_after_l": 0.0, "drive_s": dt, "drive_m": dm})
            t = start + self.unload_s
            load = 0.0
            pos = LF
            n_lf += 1

        for b in seq:
            sb = self.bin_stop[b]
            ta = t + T[pos][sb]
            lb = self.load_at(b, ta)
            if load + lb > cap and load > 0:
                landfill()
                ta = t + T[LF][sb]
                lb = self.load_at(b, ta)
            dt, dm = leg(pos, sb)
            ov = self.overflow_at(b, ta)
            over += self.weight[b] * ov
            stops.append({"kind": "bin", "bin": b, "stop": sb, "arrive_ts": ta, "depart_ts": ta + self.service_s[b],
                          "load_before_l": load, "load_after_l": load + lb, "pickup_l": lb,
                          "fill_pct": self._interp(self._mu, b, ta), "overflow_min": ov,
                          "own_beat": self.owner[b] in (-1, k), "drive_s": dt, "drive_m": dm})
            t = ta + self.service_s[b]
            load += lb
            pos = sb
        if load > 0:
            landfill()
        if pos != DEP:
            dt, dm = leg(pos, DEP)
            stops.append({"kind": "depot", "stop": DEP, "arrive_ts": t + dt, "depart_ts": t + dt,
                          "load_before_l": load, "load_after_l": load, "drive_s": dt, "drive_m": dm})
            t += dt
        return {"vehicle": k, "stops": stops, "end_ts": t, "drive_s": drive, "dist_m": dist,
                "weighted_overflow_min": over, "landfill_trips": n_lf, "cost": self.route_cost(k, seq)}


# ------------------------------------------------------------------ solution
class Solution:
    __slots__ = ("routes", "unassigned", "costs", "total")

    def __init__(self, prob: Problem, routes: list[list[int]], unassigned: list[int]):
        self.routes = routes
        self.unassigned = unassigned
        self.costs = [prob.route_cost(k, r) for k, r in enumerate(routes)]
        self.total = sum(self.costs) + sum(prob.unserved_cost[b] for b in unassigned)

    def copy(self, prob: Problem) -> "Solution":
        s = Solution.__new__(Solution)
        s.routes = [r[:] for r in self.routes]
        s.unassigned = self.unassigned[:]
        s.costs = self.costs[:]
        s.total = self.total
        return s

    def refresh(self, prob: Problem, ks=None) -> None:
        for k in (range(len(self.routes)) if ks is None else ks):
            self.costs[k] = prob.route_cost(k, self.routes[k])
        self.total = sum(self.costs) + sum(prob.unserved_cost[b] for b in self.unassigned)


# ------------------------------------------------------------------ insertion
def best_insertion(prob: Problem, sol: Solution, b: int, rng: random.Random | None = None,
                   noise: float = 0.0) -> list[tuple[float, int, int]]:
    """Best (delta, route, position) per route for inserting bin b, sorted."""
    out = []
    near = set(prob.neighbours[b][:8])
    for k, r in enumerate(sol.routes):
        base = sol.costs[k]
        # granular: positions next to near neighbours, plus both ends
        positions = {0, len(r)}
        for i, x in enumerate(r):
            if x in near:
                positions.add(i)
                positions.add(i + 1)
        if len(r) <= 12:
            positions = range(len(r) + 1)
        best = None
        for p in positions:
            c = prob.route_cost(k, r[:p] + [b] + r[p:]) - base
            if noise and rng is not None:
                c *= 1.0 + noise * (rng.random() - 0.5)
            if best is None or c < best[0]:
                best = (c, k, p)
        if best is not None:
            out.append(best)
    out.sort()
    return out


def repair_greedy(prob, sol, pending, rng, noise=0.0):
    rng.shuffle(pending)
    pending.sort(key=lambda b: -prob.unserved_cost[b])   # due / risky bins first
    for b in pending:
        opts = best_insertion(prob, sol, b, rng, noise)
        if opts and opts[0][0] < prob.unserved_cost[b]:
            _, k, p = opts[0]
            sol.routes[k].insert(p, b)
            sol.costs[k] = prob.route_cost(k, sol.routes[k])
        else:
            sol.unassigned.append(b)
    sol.refresh(prob, [])


def repair_regret(prob, sol, pending, rng, noise=0.0):
    pending = pending[:]
    cache = {b: best_insertion(prob, sol, b) for b in pending}
    while pending:
        best_b, best_score = None, -math.inf
        for b in pending:
            opts = cache[b]
            if not opts:
                score = -math.inf
            else:
                second = opts[1][0] if len(opts) > 1 else prob.unserved_cost[b]
                score = min(second, prob.unserved_cost[b]) - opts[0][0]
            if score > best_score:
                best_b, best_score = b, score
        b = best_b
        pending.remove(b)
        opts = cache.pop(b)
        if opts and opts[0][0] < prob.unserved_cost[b]:
            _, k, p = opts[0]
            sol.routes[k].insert(p, b)
            sol.costs[k] = prob.route_cost(k, sol.routes[k])
            for o in pending:   # only route k changed
                lst = [x for x in cache[o] if x[1] != k]
                kb = best_insertion_route(prob, sol, o, k)
                if kb is not None:
                    lst.append(kb)
                lst.sort()
                cache[o] = lst
        else:
            sol.unassigned.append(b)
    sol.refresh(prob, [])


def best_insertion_route(prob, sol, b, k):
    r = sol.routes[k]
    base = sol.costs[k]
    best = None
    for p in range(len(r) + 1):
        c = prob.route_cost(k, r[:p] + [b] + r[p:]) - base
        if best is None or c < best[0]:
            best = (c, k, p)
    return best


def repair_noisy(prob, sol, pending, rng, noise=0.0):
    repair_greedy(prob, sol, pending, rng, noise=0.3)


# ------------------------------------------------------------------ removal
def _remove(prob, sol, bins_to_remove):
    rm = set(bins_to_remove)
    touched = []
    for k, r in enumerate(sol.routes):
        if any(b in rm for b in r):
            sol.routes[k] = [b for b in r if b not in rm]
            touched.append(k)
    sol.refresh(prob, touched)
    return list(rm)


def destroy_random(prob, sol, q, rng):
    served = [b for r in sol.routes for b in r]
    return _remove(prob, sol, rng.sample(served, min(q, len(served))))


def destroy_worst(prob, sol, q, rng):
    gains = []
    for k, r in enumerate(sol.routes):
        for i, b in enumerate(r):
            g = sol.costs[k] - prob.route_cost(k, r[:i] + r[i + 1:])
            gains.append((g * (0.8 + 0.4 * rng.random()), b))
    gains.sort(reverse=True)
    return _remove(prob, sol, [b for _, b in gains[:q]])


def destroy_related(prob, sol, q, rng):
    served = [b for r in sol.routes for b in r]
    if not served:
        return []
    seed = rng.choice(served)
    pick = [seed]
    for nb in prob.neighbours[seed]:
        if len(pick) >= q:
            break
        if nb in served:
            pick.append(nb)
    return _remove(prob, sol, pick)


def destroy_segment(prob, sol, q, rng):
    ks = [k for k, r in enumerate(sol.routes) if r]
    if not ks:
        return []
    k = rng.choice(ks)
    r = sol.routes[k]
    L = min(q, len(r))
    i = rng.randrange(0, len(r) - L + 1)
    return _remove(prob, sol, r[i:i + L])


def destroy_offbeat(prob, sol, q, rng):
    off = [b for k, r in enumerate(sol.routes) for b in r if prob.owner[b] not in (-1, k)]
    if not off:
        return destroy_random(prob, sol, q, rng)
    rng.shuffle(off)
    return _remove(prob, sol, off[:q])


DESTROY = [destroy_random, destroy_worst, destroy_related, destroy_segment, destroy_offbeat]
REPAIR = [repair_greedy, repair_regret, repair_noisy]


# ------------------------------------------------------------------ local search
def local_search(prob: Problem, sol: Solution, deadline: float) -> Solution:
    improved = True
    while improved and time.perf_counter() < deadline:
        improved = False
        # 2-opt inside each route
        for k, r in enumerate(sol.routes):
            n = len(r)
            for i in range(n - 1):
                for j in range(i + 2, n + 1):
                    cand = r[:i] + r[i:j][::-1] + r[j:]
                    c = prob.route_cost(k, cand)
                    if c < sol.costs[k] - 1e-6:
                        sol.routes[k] = r = cand
                        sol.costs[k] = c
                        improved = True
            if time.perf_counter() > deadline:
                break
        # relocate any bin to its best position anywhere (also re-try unassigned)
        for b in [x for r in sol.routes for x in r] + sol.unassigned[:]:
            k0 = next((k for k, r in enumerate(sol.routes) if b in r), None)
            trial = sol.copy(prob)
            if k0 is None:
                trial.unassigned.remove(b)
            else:
                trial.routes[k0].remove(b)
                trial.costs[k0] = prob.route_cost(k0, trial.routes[k0])
            opts = best_insertion(prob, trial, b)
            if opts and opts[0][0] < prob.unserved_cost[b]:
                _, k, p = opts[0]
                trial.routes[k].insert(p, b)
                trial.refresh(prob, [k])
            else:
                trial.unassigned.append(b)
                trial.refresh(prob, [])
            if trial.total < sol.total - 1e-6:
                sol = trial
                improved = True
        # swap two bins between routes
        for k1 in range(len(sol.routes)):
            for k2 in range(k1 + 1, len(sol.routes)):
                r1, r2 = sol.routes[k1], sol.routes[k2]
                for i in range(len(r1)):
                    for j in range(len(r2)):
                        a, b = r1[i], r2[j]
                        n1 = r1[:i] + [b] + r1[i + 1:]
                        n2 = r2[:j] + [a] + r2[j + 1:]
                        c1, c2 = prob.route_cost(k1, n1), prob.route_cost(k2, n2)
                        if c1 + c2 < sol.costs[k1] + sol.costs[k2] - 1e-6:
                            sol.routes[k1], sol.routes[k2] = r1, r2 = n1, n2
                            sol.costs[k1], sol.costs[k2] = c1, c2
                            improved = True
                if time.perf_counter() > deadline:
                    break
        sol.refresh(prob, [])
    return sol


# ------------------------------------------------------------------ construction
def initial_solution(prob: Problem, rng: random.Random) -> Solution:
    """Each truck takes its own beat in nearest-neighbour order; the rest by regret insertion."""
    nv = len(prob.vehicles)
    routes: list[list[int]] = [[] for _ in range(nv)]
    rest = []
    for k in range(nv):
        mine = [b for b in range(prob.nb) if prob.owner[b] == k]
        pos = prob.vehicles[k].start_pos
        while mine:
            b = min(mine, key=lambda x: prob.Tl[pos][prob.bin_stop[x]])
            routes[k].append(b)
            pos = prob.bin_stop[b]
            mine.remove(b)
    rest = [b for b in range(prob.nb) if prob.owner[b] < 0 or prob.owner[b] >= nv]
    sol = Solution(prob, routes, [])
    # drop bins from routes that run past the shift until feasible
    for k in range(nv):
        while sol.routes[k] and prob.route_cost(k, sol.routes[k]) >= prob.params.overtime_fixed:
            worst = max(sol.routes[k], key=lambda b: -prob.unserved_cost[b])
            sol.routes[k].remove(worst)
            rest.append(worst)
        sol.refresh(prob, [k])
    repair_regret(prob, sol, rest, rng)
    return sol


# ------------------------------------------------------------------ ALNS
@dataclass
class SolveStats:
    iterations: int = 0
    seconds: float = 0.0
    initial_cost: float = 0.0
    best_cost: float = 0.0
    accepted: int = 0
    improvements: int = 0
    operator_weights: dict = field(default_factory=dict)


def solve(prob: Problem, seconds: float = 8.0, max_iter: int = 5000, seed: int = 1,
          start: Solution | None = None, patience: int | None = None,
          progress=None) -> tuple[Solution, SolveStats]:
    """patience: stop early once this many iterations in a row found no better plan
    (seconds is then only a safety cap). progress(iterations, best_solution) is called about twice
    a second so a user interface can show how the search is going."""
    rng = random.Random(seed)
    t_start = time.perf_counter()
    deadline = t_start + seconds
    cur = start.copy(prob) if start else initial_solution(prob, rng)
    best = cur.copy(prob)
    stats = SolveStats(initial_cost=cur.total)
    if prob.nb == 0:
        stats.best_cost = best.total
        return best, stats
    wd = [1.0] * len(DESTROY)
    wr = [1.0] * len(REPAIR)
    sd = [0.0] * len(DESTROY)
    sr = [0.0] * len(REPAIR)
    nd = [0] * len(DESTROY)
    nr = [0] * len(REPAIR)
    # Start so that a move 1 % worse (on route costs, not penalties) is accepted ~37 % of the time.
    T0 = max(1.0, 0.01 * sum(c for c in cur.costs if c < prob.params.overtime_fixed))
    q_max = max(2, min(15, int(0.35 * prob.nb)))
    it = 0
    last_better = 0
    next_report = t_start
    ls_budget = 0.15 * seconds if patience is None else min(3.0, 0.15 * seconds)
    while it < max_iter and time.perf_counter() < deadline - ls_budget:
        if patience is not None and it - last_better > patience and it >= 2 * patience:
            break
        it += 1
        frac = (time.perf_counter() - t_start) / max(1e-9, seconds - ls_budget)
        temp = T0 * (1 - min(frac, 1.0)) + 1e-3
        di = rng.choices(range(len(DESTROY)), wd)[0]
        ri = rng.choices(range(len(REPAIR)), wr)[0]
        cand = cur.copy(prob)
        q = rng.randint(2, q_max)
        removed = DESTROY[di](prob, cand, q, rng)
        pending = removed + cand.unassigned
        cand.unassigned = []
        REPAIR[ri](prob, cand, pending, rng)
        score = 0.0
        if cand.total < best.total - 1e-6:
            best = cand.copy(prob)
            cur = cand
            score = 33.0
            stats.improvements += 1
            last_better = it
        elif cand.total < cur.total - 1e-6:
            cur = cand
            score = 9.0
        elif rng.random() < math.exp(-(cand.total - cur.total) / temp):
            cur = cand
            score = 13.0
            stats.accepted += 1
        sd[di] += score
        sr[ri] += score
        nd[di] += 1
        nr[ri] += 1
        if progress is not None and time.perf_counter() >= next_report:
            next_report = time.perf_counter() + 0.5
            progress(it, best)
        if it % 50 == 0:
            for w, s, n in ((wd, sd, nd), (wr, sr, nr)):
                for i in range(len(w)):
                    if n[i]:
                        w[i] = 0.8 * w[i] + 0.2 * s[i] / n[i]
                    w[i] = max(w[i], 0.05)
                    s[i] = 0.0
                    n[i] = 0
    best = local_search(prob, best, time.perf_counter() + max(ls_budget, 0.5))
    stats.iterations = it
    stats.seconds = time.perf_counter() - t_start
    stats.best_cost = best.total
    stats.operator_weights = {"destroy": {f.__name__: round(w, 2) for f, w in zip(DESTROY, wd)},
                              "repair": {f.__name__: round(w, 2) for f, w in zip(REPAIR, wr)}}
    return best, stats
