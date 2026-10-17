"""Study area: bin register, fleet, depot and landfill for the Sanpada node.

Bin *sites* are real landmarks taken from today's OpenStreetMap extract.
Each bin is placed at the kerb of the nearest road where a truck may stop
(never on a flyover, motorway or link). Capacities, priorities, sensors and
collection frequencies are demonstration values a ward office would edit.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import dataclass

from . import db, geo, osm

# Sanpada node incl. the Juinagar station area (south, west, north, east).
# Vashi sectors west of the Palm Beach / Vashi-Sanpada link road are excluded.
SERVICE_AREA = (19.047, 73.002, 19.0685, 73.021)

# Roads where a truck may stop to empty a bin.
STOPPABLE = {"secondary", "tertiary", "unclassified", "residential", "living_street", "service", "road"}
# Ramps and underpasses are often not tagged bridge/tunnel in OSM; their names give them away.
NO_STOP_NAME = re.compile(r"flyover|bridge|underpass|subway|\bFOB\b|ramp", re.I)
TARGET_BINS = 50


@dataclass(frozen=True)
class SiteType:
    key: str
    label: str
    quota: int
    capacity_l: int
    priority: int          # 1 = highest
    every_days: int
    rest_days: str = ""    # weekdays without collection, 0 = Monday ... 6 = Sunday ("6" = not on Sundays)


SITE_TYPES = [
    SiteType("hospital", "Hospital", 3, 1100, 1, 1),
    SiteType("clinic", "Clinic / nursing home", 2, 660, 1, 1),
    SiteType("station", "Railway station", 2, 1100, 1, 1),
    SiteType("bus_stand", "Bus stand", 2, 1100, 1, 1),
    SiteType("supermarket", "Supermarket", 1, 2400, 1, 1),
    SiteType("gaothan", "Village core (gaothan)", 2, 2400, 1, 1),
    SiteType("school", "School", 8, 660, 1, 1, "6"),          # closed on Sundays: no collection
    SiteType("college", "College", 2, 1100, 2, 1),
    SiteType("temple", "Temple", 2, 660, 2, 1),
    SiteType("worship", "Church / gurudwara", 2, 660, 2, 1),
    SiteType("eatery", "Eateries", 2, 1100, 2, 1),
    SiteType("public_office", "Public office", 1, 660, 2, 2),
    SiteType("housing", "Housing society", 10, 1100, 2, 1),
    SiteType("park", "Park", 6, 660, 3, 2),
    SiteType("ground", "Playground", 5, 660, 3, 2),
]
SITE = {s.key: s for s in SITE_TYPES}

FLEET = [
    # id, name, capacity (L), colour (vehicle colours never red/orange/amber/green)
    ("TRK-1", "Truck 1", 12000, "#2563EB"),   # blue
    ("TRK-2", "Truck 2", 12000, "#7C3AED"),   # purple
    ("TRK-3", "Truck 3 (small)", 6000, "#0D9488"),  # teal
]

DEPOT = {
    "name": "Sanpada vehicle depot (demonstration location - confirm with NMMC)",
    "lat": 19.06506, "lon": 73.01061, "unload_min": 0.0,
    "note": "Placed next to Sanpada station parking for the demonstration. "
            "Set the real NMMC depot under Settings > Vehicles & depot.",
}
LANDFILL_FALLBACK = {"lat": 19.08077, "lon": 73.02288}


def classify(tags: dict) -> str | None:
    name = tags.get("name", "")
    if tags.get("railway") == "station":
        return "station"
    a = tags.get("amenity")
    if a == "bus_station":
        return "bus_stand"
    if a == "hospital":
        return "hospital"
    if a in ("clinic", "doctors"):
        return "clinic"
    if a in ("school", "kindergarten"):
        return "school"
    if a == "college":
        return "college"
    if a == "place_of_worship":
        hindu = tags.get("religion") == "hindu" or re.search(r"mandir|temple", name, re.I)
        return "temple" if hindu else "worship"
    if a in ("restaurant", "fast_food", "food_court"):
        return "eatery"
    if a in ("police", "post_office", "townhall", "community_centre", "library"):
        return "public_office"
    if a == "marketplace":
        return "supermarket"
    if tags.get("shop") in ("supermarket", "mall", "department_store"):
        return "supermarket"
    lz = tags.get("leisure")
    if lz in ("park", "garden"):
        return "park"
    if lz in ("playground", "pitch", "stadium", "sports_centre"):
        return "ground"
    if tags.get("landuse") == "residential" or tags.get("building") in ("apartments", "residential"):
        return "housing"
    if tags.get("place") == "locality" and re.search(r"village", name, re.I):
        return "gaothan"
    return None


def _in_area(lat: float, lon: float, area=SERVICE_AREA) -> bool:
    s, w, n, e = area
    return s <= lat <= n and w <= lon <= e


class KerbIndex:
    """Nearest stoppable road segment for a point (brute force; ~10k segments)."""

    def __init__(self, roads: dict):
        nodes = {e["id"]: (e["lat"], e["lon"]) for e in roads["elements"] if e["type"] == "node"}
        self.lat0 = sum(SERVICE_AREA[::2]) / 2
        self.segs: list[tuple[float, float, float, float, str | None, str]] = []
        for w in roads["elements"]:
            if w["type"] != "way":
                continue
            t = w.get("tags", {})
            hw = t.get("highway", "")
            if hw not in STOPPABLE or t.get("bridge") or t.get("tunnel") or t.get("layer", "0") not in ("0", ""):
                continue
            if t.get("service") in ("parking_aisle",) or NO_STOP_NAME.search(t.get("name", "")):
                continue
            pts = [nodes[n] for n in w["nodes"] if n in nodes]
            for (la1, lo1), (la2, lo2) in zip(pts, pts[1:]):
                ax, ay = geo.to_xy(la1, lo1, self.lat0)
                bx, by = geo.to_xy(la2, lo2, self.lat0)
                self.segs.append((ax, ay, bx, by, t.get("name"), hw))

    def nearest(self, lat: float, lon: float, named_only: bool = False, max_m: float = 300.0):
        px, py = geo.to_xy(lat, lon, self.lat0)
        best = None
        for ax, ay, bx, by, name, hw in self.segs:
            if named_only and not name:
                continue
            if abs(ax - px) > max_m and abs(bx - px) > max_m:
                continue
            x, y, _, d = geo.project_to_segment(px, py, ax, ay, bx, by)
            if d <= max_m and (best is None or d < best[2]):
                best = (x, y, d, name, hw)
        if best is None:
            return None
        la, lo = geo.from_xy(best[0], best[1], self.lat0)
        return {"lat": la, "lon": lo, "dist_m": best[2], "name": best[3], "highway": best[4]}


def candidate_sites(pois: dict, kerb: KerbIndex) -> list[dict]:
    out, seen = [], set()
    for e in pois["elements"]:
        t = e.get("tags", {})
        c = e.get("center", e)
        if "lat" not in c or not t.get("name"):
            continue
        st = classify(t)
        if st is None or not _in_area(c["lat"], c["lon"]):
            continue
        key = (t["name"].lower(), st)
        if key in seen:     # e.g. station mapped as both a node and a way
            continue
        seen.add(key)
        k = kerb.nearest(c["lat"], c["lon"])
        if k is None:
            continue
        street = k["name"]
        if not street:
            named = kerb.nearest(c["lat"], c["lon"], named_only=True, max_m=200)
            street = named["name"] if named else f"{k['highway'].replace('_', ' ')} road near {t['name']}"
        landmark = t["name"]
        if st == "station":
            landmark = f"{landmark} railway station"
        elif st == "bus_stand":
            landmark = f"{landmark} bus stand"
        out.append({
            "landmark": landmark, "site_type": st, "osm_ref": f"{e['type']}/{e['id']}",
            "poi_lat": c["lat"], "poi_lon": c["lon"],
            "lat": round(k["lat"], 6), "lon": round(k["lon"], 6), "street": street,
            "kerb_offset_m": round(k["dist_m"], 1),
        })
    return out


def select_bins(cands: list[dict], min_spacing_m: float = 50.0) -> list[dict]:
    """Per site type, pick its quota spreading bins out (greedy farthest point)."""
    chosen: list[dict] = []

    def pick(pool: list[dict]) -> bool:
        best, best_d = None, -1.0
        for c in pool:
            d = min((geo.haversine_m(c["lat"], c["lon"], b["lat"], b["lon"]) for b in chosen),
                    default=1e9)
            if d >= min_spacing_m and d > best_d:
                best, best_d = c, d
        if best is None:
            return False
        chosen.append(best)
        pool.remove(best)
        return True

    pools = {st.key: sorted((c for c in cands if c["site_type"] == st.key), key=lambda c: c["osm_ref"])
             for st in SITE_TYPES}
    for st in SITE_TYPES:
        for _ in range(st.quota):
            if not pick(pools[st.key]):
                break
    # Categories that ran short (too few mapped sites) are topped up with housing societies.
    for key in ("housing", "school", "eatery", "park"):
        while len(chosen) < TARGET_BINS and pick(pools[key]):
            pass
    chosen.sort(key=lambda b: (-round(b["lat"], 3), b["lon"]))
    for i, b in enumerate(chosen, 1):
        st = SITE[b["site_type"]]
        b["id"] = f"SNP-{i:03d}"
        b["capacity_l"] = st.capacity_l
        b["priority"] = st.priority
        b["collect_every_days"] = st.every_days
        b["rest_days"] = st.rest_days
        # Sensors on all priority-1 sites and on large bins; manual readings elsewhere.
        b["has_sensor"] = int(st.priority == 1 or st.capacity_l >= 1100)
    return chosen


def find_landfill(pois: dict) -> dict:
    for e in pois["elements"]:
        t = e.get("tags", {})
        if t.get("landuse") == "landfill" and re.search(r"turbhe", t.get("name", ""), re.I):
            c = e.get("center", e)
            return {"lat": c["lat"], "lon": c["lon"], "osm_ref": f"{e['type']}/{e['id']}"}
    return dict(LANDFILL_FALLBACK, osm_ref=None)


def setup(conn: sqlite3.Connection, replace: bool = False) -> dict:
    """Create bins, fleet, depot and landfill from the downloaded OSM extract.

    Existing rows are kept unless replace=True (so edits made in Settings survive).
    """
    roads, pois = osm.load(osm.ROADS_FILE), osm.load(osm.POIS_FILE)
    kerb = KerbIndex(roads)
    bins = select_bins(candidate_sites(pois, kerb))
    lf = find_landfill(pois)
    t = db.now()
    with db.tx(conn):
        if replace:
            for table in ("bins", "vehicles", "places"):
                conn.execute(f"DELETE FROM {table}")
        for b in bins:
            conn.execute(
                "INSERT OR IGNORE INTO bins(id, landmark, street, site_type, lat, lon, capacity_l, priority,"
                " has_sensor, active, collect_every_days, rest_days, osm_ref, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,1,?,?,?,?)",
                (b["id"], b["landmark"], b["street"], b["site_type"], b["lat"], b["lon"], b["capacity_l"],
                 b["priority"], b["has_sensor"], b["collect_every_days"], b["rest_days"], b["osm_ref"], t))
        for vid, name, cap, color in FLEET:
            conn.execute("INSERT OR IGNORE INTO vehicles(id, name, capacity_l, shift_start, shift_hours, color, active)"
                         " VALUES (?,?,?,?,?,?,1)", (vid, name, cap, "06:00", 4.0, color))
        conn.execute("INSERT OR IGNORE INTO places(kind, name, lat, lon, unload_min, note) VALUES ('depot',?,?,?,?,?)",
                     (DEPOT["name"], DEPOT["lat"], DEPOT["lon"], DEPOT["unload_min"], DEPOT["note"]))
        conn.execute(
            "INSERT OR IGNORE INTO places(kind, name, lat, lon, unload_min, note) VALUES ('landfill',?,?,?,?,?)",
            ("Turbhe sanitary landfill (NMMC) - confirm with NMMC", lf["lat"], lf["lon"], 20.0,
             f"Approximate: centre of OpenStreetMap landfill polygon {lf['osm_ref']}. "
             "Gate position and the ~20 min unloading time must be confirmed with NMMC."))
        db.set_meta(conn, "osm_provenance", json.dumps({"roads": osm.describe(roads), "pois": osm.describe(pois)}))
        db.bump_version(conn)
    return {"bins": len(bins), "landfill": lf}


if __name__ == "__main__":
    roads, pois = osm.load(osm.ROADS_FILE), osm.load(osm.POIS_FILE)
    kerb = KerbIndex(roads)
    cands = candidate_sites(pois, kerb)
    sel = select_bins(cands)
    from collections import Counter
    print(len(cands), "candidates;", len(sel), "selected", Counter(b["site_type"] for b in sel))
    for b in sel:
        print(f"{b['id']} {b['site_type']:13s} {b['landmark'][:40]:40s} | {b['street'][:32]:32s} "
              f"{b['lat']:.5f},{b['lon']:.5f} off={b['kerb_offset_m']}m")
