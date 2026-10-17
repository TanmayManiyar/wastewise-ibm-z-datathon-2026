"""Synthetic demonstration history (clearly labelled: every row has synthetic=1).

No real fill-level data exists for Sanpada, so this module simulates 180 days
of hourly bin filling so the forecaster, anomaly detector and planner have
something realistic to work on. It models:
  * site-specific daily and weekly patterns (stations peak at commute hours,
    schools are quiet on Sundays, supermarkets busy at weekends, ...),
  * festivals (Ganeshotsav, Navratri, Diwali, ...) and the monsoon,
  * sensor noise, saturation at 100 %, dropouts and faults (stuck, spikes,
    drift) with ground-truth fault labels for evaluating the anomaly model,
  * the collection routine the ward used before WasteWise: one round a day in
    a fixed nearest-bin order, with occasional misses,
  * supervisor visual estimates for bins without sensors, crew fill estimates,
    and a few citizen complaints when bins overflow.
The true (unobservable) fill including overflow is kept in synthetic_truth
for evaluation only.
"""
from __future__ import annotations

import json
import math
import sqlite3
import zlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import numpy as np

from . import calendar_in, config, db, geo

# Mean fraction of the bin's capacity generated per day, by site type.
DAILY_FILL = {
    "supermarket": 0.8, "station": 0.75, "bus_stand": 0.7, "gaothan": 0.8, "hospital": 0.65,
    "clinic": 0.5, "school": 0.55, "college": 0.5, "temple": 0.5, "worship": 0.4,
    "eatery": 0.75, "public_office": 0.4, "housing": 0.7, "park": 0.4, "ground": 0.35,
}

# Hour-of-day profile as (peak hour, width h, weight) bumps on a small base.
PROFILES = {
    "housing": [(8, 1.5, 1.0), (20, 2.0, 0.8)],
    "station": [(9, 1.5, 1.0), (19, 2.0, 1.0)],
    "bus_stand": [(9, 1.5, 1.0), (19, 2.0, 0.9)],
    "supermarket": [(12, 2.0, 0.6), (19, 2.0, 1.0)],
    "eatery": [(13, 1.5, 0.8), (21, 1.5, 1.0)],
    "school": [(10, 2.5, 1.0), (13, 1.0, 0.6)],
    "college": [(12, 3.0, 1.0)],
    "hospital": [(11, 3.0, 1.0), (18, 2.0, 0.6)],
    "clinic": [(11, 1.5, 1.0), (19, 1.5, 0.8)],
    "temple": [(7, 1.5, 1.0), (19, 1.5, 0.8)],
    "worship": [(10, 2.0, 1.0), (18, 2.0, 0.4)],
    "park": [(7, 1.5, 0.8), (19, 2.0, 1.0)],
    "ground": [(7, 1.5, 0.6), (18, 2.0, 1.0)],
    "gaothan": [(8, 2.0, 1.0), (19, 2.0, 0.9)],
    "public_office": [(13, 3.0, 1.0)],
}
BASE_LEVEL = {"school": 0.01, "college": 0.02, "public_office": 0.02}

# Monday .. Sunday multipliers.
WEEKLY = {
    "school": [1, 1, 1, 1, 1, 0.6, 0.05], "college": [1, 1, 1, 1, 1, 0.5, 0.05],
    "station": [1.05, 1, 1, 1, 1.05, 0.8, 0.65], "bus_stand": [1.05, 1, 1, 1, 1.05, 0.8, 0.65],
    "supermarket": [0.85, 0.85, 0.9, 0.9, 1.0, 1.3, 1.4], "eatery": [0.9, 0.9, 0.9, 0.95, 1.1, 1.3, 1.3],
    "park": [0.8, 0.8, 0.8, 0.8, 0.9, 1.3, 1.6], "ground": [0.8, 0.8, 0.8, 0.8, 0.9, 1.3, 1.6],
    "worship": [0.7, 0.7, 0.7, 0.7, 0.8, 0.9, 2.0], "temple": [1, 1.2, 1, 1.1, 1, 1.3, 1.1],
    "hospital": [1, 1, 1, 1, 1, 0.9, 0.75], "clinic": [1, 1, 1, 1, 1, 0.9, 0.5],
    "public_office": [1, 1, 1, 1, 1, 0.5, 0.1], "housing": [1, 1, 1, 1, 1, 1.1, 1.2],
    "gaothan": [1, 1, 1, 1, 1, 1.1, 1.15],
}
RAIN_SENSITIVE = {"station": 0.88, "bus_stand": 0.88, "park": 0.75, "ground": 0.7, "eatery": 0.9}
WET_WASTE = {"housing": 1.06, "gaothan": 1.06, "eatery": 1.05}

COMPLAINT_TEMPLATES = {
    "en": ["Garbage bin near {lm} is overflowing, please clear it",
           "Bin at {lm} full since morning, very bad smell",
           "Waste lying on the road next to {lm}, dustbin not emptied"],
    "hi": ["{lm} के पास कूड़ेदान भर गया है, कृपया साफ़ करें",
           "{lm} के पास कचरा सड़क पर गिर रहा है, बहुत बदबू आ रही है"],
    "mr": ["{lm} जवळ कचराकुंडी भरून वाहत आहे, कृपया उचला",
           "{lm} जवळ कचरा रस्त्यावर पडला आहे, वास येत आहे"],
    "mixed": ["{lm} ke paas dustbin full hai, kachra road pe aa raha hai",
              "{lm} jawal kachra bharla aahe, please uchla"],
}


@dataclass
class BinSpec:
    id: str
    site_type: str
    capacity_l: float
    has_sensor: bool
    every_days: int
    daily_l: float
    profile: np.ndarray  # 24 weights summing to 1


def _rng(seed: int, key: str) -> np.random.Generator:
    return np.random.default_rng([seed, zlib.crc32(key.encode())])


def hourly_profile(site_type: str) -> np.ndarray:
    h = np.arange(24, dtype=float)
    w = np.full(24, BASE_LEVEL.get(site_type, 0.08))
    for peak, width, weight in PROFILES.get(site_type, [(12, 4.0, 1.0)]):
        d = np.minimum(np.abs(h - peak), 24 - np.abs(h - peak))
        w += weight * np.exp(-0.5 * (d / width) ** 2)
    if site_type not in ("hospital", "station", "bus_stand"):
        w[1:5] *= 0.2   # little activity between 01:00 and 05:00
    return w / w.sum()


def bin_specs(bins: list[dict], seed: int) -> list[BinSpec]:
    out = []
    for b in bins:
        r = _rng(seed, "spec" + b["id"])
        # Heterogeneity: some bins are too small for their site, some too big.
        u = DAILY_FILL.get(b["site_type"], 0.7) * float(r.lognormal(0.0, 0.22))
        out.append(BinSpec(b["id"], b["site_type"], float(b["capacity_l"]), bool(b["has_sensor"]),
                           int(b["collect_every_days"]), u * float(b["capacity_l"]),
                           hourly_profile(b["site_type"])))
    return out


def routine_order(bins: list[dict], depot: dict) -> list[str]:
    """The pre-WasteWise routine: visit the nearest unvisited bin, from the depot."""
    left = {b["id"]: b for b in bins}
    cur = (depot["lat"], depot["lon"])
    order = []
    while left:
        bid = min(left, key=lambda k: geo.haversine_m(cur[0], cur[1], left[k]["lat"], left[k]["lon"]))
        order.append(bid)
        cur = (left[bid]["lat"], left[bid]["lon"])
        del left[bid]
    return order


def plan_day_for(end_local: datetime, shift_start: str, shift_hours: float) -> date:
    """The day WasteWise is planning: today until the shift ends, else tomorrow."""
    hh, mm = map(int, shift_start.split(":"))
    shift_end = end_local.replace(hour=hh, minute=mm, second=0, microsecond=0) + timedelta(hours=shift_hours)
    return end_local.date() if end_local < shift_end else end_local.date() + timedelta(days=1)


def generate(conn: sqlite3.Connection, end_ts: int | None = None, days: int = 180, seed: int = 2026,
             complaint_rate: float = 0.12) -> dict:
    """Replace all synthetic rows with a fresh history ending at end_ts (default now)."""
    end_ts = end_ts or db.now()
    bins = db.bins(conn)
    vehicles = db.vehicles(conn)
    places = db.places(conn)
    settings = db.get_settings(conn)
    if not bins or "depot" not in places:
        raise RuntimeError("Set up the area (bins, depot) before generating history")

    end_local = datetime.fromtimestamp(end_ts, config.IST).replace(minute=0, second=0, microsecond=0)
    H = days * 24
    start_local = end_local - timedelta(hours=H - 1)
    hour_ts = np.array([int((start_local + timedelta(hours=i)).timestamp()) for i in range(H)])
    local = [start_local + timedelta(hours=i) for i in range(H)]
    hod = np.array([d.hour for d in local])
    dow = np.array([d.weekday() for d in local])
    dates = [d.date() for d in local]
    plan_day = plan_day_for(datetime.fromtimestamp(end_ts, config.IST), settings["shift_start"],
                            settings["shift_hours"])

    specs = bin_specs(bins, seed)
    by_id = {b["id"]: b for b in bins}
    order = routine_order(bins, places["depot"])
    # The routine splits the fixed order between trucks in proportion to capacity.
    caps = np.array([v["capacity_l"] for v in vehicles], dtype=float)
    cuts = np.round(np.cumsum(caps / caps.sum()) * len(order)).astype(int)
    truck_of, slot_of = {}, {}
    start = 0
    for v, cut in zip(vehicles, cuts):
        for k, bid in enumerate(order[start:cut]):
            truck_of[bid], slot_of[bid] = v["id"], k
        start = cut
    hh, mm = map(int, settings["shift_start"].split(":"))

    # Day-level factors shared by all bins.
    uniq_days = sorted(set(dates))
    day_rng = _rng(seed, "days")
    rain = {d: (calendar_in.is_monsoon(d) and day_rng.random() < 0.6) for d in uniq_days}
    day_noise = {d: float(day_rng.lognormal(0, 0.10)) for d in uniq_days}
    trend = {d: 1.0 + 0.03 * (i / max(1, len(uniq_days) - 1)) for i, d in enumerate(uniq_days)}

    readings, events, truth, complaints = [], [], [], []
    final_state = {}
    stats = {"overflow_bin_hours": 0, "faults": 0, "missed": 0, "collections": 0}

    for sp in specs:
        r = _rng(seed, "sim" + sp.id)
        week = np.array(WEEKLY.get(sp.site_type, [1] * 7))
        day_f = {}
        for d in uniq_days:
            f = calendar_in.festival_multiplier(d, sp.site_type) * day_noise[d] * trend[d]
            if rain[d]:
                f *= RAIN_SENSITIVE.get(sp.site_type, 1.0) * WET_WASTE.get(sp.site_type, 1.0)
            day_f[d] = f
        gen = np.array([sp.daily_l * day_f[dates[i]] for i in range(H)]) * sp.profile[hod] * week[dow] * 7 / week.sum()
        gen *= r.gamma(4.0, 0.25, H)

        # Collection schedule from the routine.
        visit_min = 10 + slot_of[sp.id] * 9   # minutes after shift start
        vol = np.empty(H)
        v = float(r.uniform(0.0, 0.3)) * sp.capacity_l
        last_collect_day = dates[0] - timedelta(days=int(r.integers(0, sp.every_days)))
        overflow_since = None
        for i in range(H):
            vol[i] = v
            d = dates[i]
            collect_here = False
            if hod[i] == hh + (mm + visit_min) // 60 and d < plan_day and (d - last_collect_day).days >= sp.every_days:
                miss_p = 0.06 if calendar_in.is_monsoon(d) else 0.04
                if r.random() < miss_p:
                    events.append((int(hour_ts[i] + ((mm + visit_min) % 60) * 60), sp.id, truck_of[sp.id],
                                   None, "could_not_reach", None, 1, "synthetic", "routine round (synthetic)"))
                    stats["missed"] += 1
                    # skipped today; the routine tries again tomorrow
                    last_collect_day = d - timedelta(days=sp.every_days - 1)
                else:
                    collect_here = True
            if collect_here:
                m = (mm + visit_min) % 60 + float(np.clip(r.normal(0, 8), -25, 25))
                m = float(np.clip(m, 0, 59))
                before = v + gen[i] * m / 60
                fill_before = min(before / sp.capacity_l * 100, 100.0)
                crew_est = float(np.clip(round((fill_before + r.normal(0, 8)) / 25) * 25, 0, 100))
                ts_c = int(hour_ts[i] + m * 60)
                events.append((ts_c, sp.id, truck_of[sp.id], None, "collected", crew_est, 1, "synthetic",
                               "routine round (synthetic)"))
                stats["collections"] += 1
                if overflow_since is not None:
                    for c in complaints:
                        if c["bin_id"] == sp.id and c["resolved_at"] is None:
                            c["resolved_at"] = ts_c
                overflow_since = None
                v = float(r.uniform(0.0, 0.03)) * sp.capacity_l + gen[i] * (60 - m) / 60
                last_collect_day = d
            else:
                v = v + gen[i]
            pct = v / sp.capacity_l * 100
            if pct > 100:
                stats["overflow_bin_hours"] += 1
            if pct > 105:
                if overflow_since is None:
                    overflow_since = i
                elif i - overflow_since == 3 and r.random() < complaint_rate:
                    lang = str(r.choice(["en", "hi", "mr", "mixed"], p=[0.4, 0.25, 0.2, 0.15]))
                    tpl = str(r.choice(COMPLAINT_TEMPLATES[lang]))
                    complaints.append({"ts": int(hour_ts[i] + r.integers(0, 3600)), "bin_id": sp.id,
                                       "lang": lang, "text": tpl.format(lm=by_id[sp.id]["landmark"]),
                                       "resolved_at": None})
        final_state[sp.id] = float(v)
        true_pct = vol / sp.capacity_l * 100
        truth.extend(zip([sp.id] * H, hour_ts.tolist(), np.round(true_pct, 2).tolist()))

        if sp.has_sensor:
            obs = np.minimum(true_pct, 100.0) + r.normal(0, 2.0, H)
            fault = np.zeros(H, dtype=int)
            mon = np.array([calendar_in.is_monsoon(d) for d in dates])
            # Stuck sensor: repeats one value for 1-3 days.
            for _ in range(r.poisson(days / 120)):
                s = int(r.integers(0, H - 24)); L = int(r.integers(12, 48))
                obs[s:s + L] = obs[s]; fault[s:s + L] = 1
            # Drift: reading creeps upwards over 5-10 days.
            for _ in range(r.poisson(days / 180)):
                s = int(r.integers(0, H - 120)); L = int(r.integers(72, 168))
                L = min(L, H - s)
                obs[s:s + L] += np.linspace(0, float(r.uniform(15, 35)), L); fault[s:s + L] = 1
            # Spikes (more in the monsoon: water on the ultrasonic sensor).
            p_spike = np.where(mon, 0.006, 0.003)
            spikes = r.random(H) < p_spike
            obs[spikes] = r.choice([0.0, 100.0], spikes.sum()); fault[spikes] = 1
            obs = np.clip(obs, 0, 100)
            # Dropouts: random missing hours and occasional outages.
            keep = r.random(H) > 0.02
            for _ in range(r.poisson(days / 100)):
                s = int(r.integers(0, H - 48)); keep[s:s + int(r.integers(6, 48))] = False
            offs = r.integers(0, 300, H)
            for i in np.nonzero(keep)[0]:
                readings.append((sp.id, int(hour_ts[i] + offs[i]), round(float(obs[i]), 1), "sensor", 1,
                                 int(fault[i]), "synthetic", None))
            stats["faults"] += int(fault[keep].sum())
        else:
            # Supervisor walks past most days and estimates to the nearest 10 %.
            for i in np.nonzero((hod >= 11) & (hod <= 17))[0]:
                if hod[i] == 11 and r.random() < 0.6:
                    j = min(H - 1, i + int(r.integers(0, 6)))
                    est = float(np.clip(round((min(true_pct[j], 100) + r.normal(0, 7)) / 10) * 10, 0, 100))
                    readings.append((sp.id, int(hour_ts[j] + r.integers(0, 3600)), est, "supervisor", 1, 0,
                                     "synthetic", "visual estimate (synthetic)"))

    with db.tx(conn):
        clear_synthetic(conn)
        conn.executemany("INSERT INTO readings(bin_id, ts, fill_pct, source, synthetic, fault, user, note)"
                         " VALUES (?,?,?,?,?,?,?,?)", readings)
        conn.executemany("INSERT INTO events(ts, bin_id, vehicle_id, plan_id, kind, fill_before, synthetic, user, note)"
                         " VALUES (?,?,?,?,?,?,?,?,?)", events)
        conn.executemany("INSERT INTO synthetic_truth(bin_id, ts, fill_pct) VALUES (?,?,?)", truth)
        for c in complaints:
            conn.execute("INSERT INTO complaints(ts, text, lang, bin_id, problem, status, reporter, created_by,"
                         " resolved_by, resolved_at, parsed, synthetic) VALUES (?,?,?,?,?,?,?,?,?,?,?,1)",
                         (c["ts"], c["text"], c["lang"], c["bin_id"], "overflowing",
                          "resolved" if c["resolved_at"] else "open", "citizen (synthetic)", "synthetic",
                          "synthetic" if c["resolved_at"] else None, c["resolved_at"], None))
        info = {"seed": seed, "days": days, "start_ts": int(hour_ts[0]), "end_ts": int(hour_ts[-1]),
                "plan_day": plan_day.isoformat(), "generated_at": db.now(),
                "rows": {"readings": len(readings), "events": len(events), "complaints": len(complaints)},
                "stats": stats, "label": "SYNTHETIC DEMONSTRATION DATA"}
        db.set_meta(conn, "synthetic_info", json.dumps(info))
        db.set_meta(conn, "synthetic_state", json.dumps({"ts": int(hour_ts[-1]) + 3600, "vol_l": final_state}))
        db.bump_version(conn)
    return info


def clear_synthetic(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM readings WHERE synthetic = 1")
    conn.execute("DELETE FROM events WHERE synthetic = 1")
    conn.execute("DELETE FROM complaints WHERE synthetic = 1")
    conn.execute("DELETE FROM synthetic_truth")
