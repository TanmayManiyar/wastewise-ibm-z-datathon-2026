"""Download the study area from OpenStreetMap through the Overpass API.

Two extracts are saved under data/:
  * roads: every drivable highway way (with its nodes) in config.BBOX,
  * pois:  named landmarks in the Sanpada node (stations, markets, hospitals,
           schools, parks, places of worship, housing societies, ...),
plus the Turbhe landfill polygon. Data (c) OpenStreetMap contributors, ODbL.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import config

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
USER_AGENT = "WasteWise/0.1 (IBM Datathon 2026; ward waste-collection planner)"

# Sanpada node (bins are placed only here); roads cover a wider box that
# reaches the landfill.
SANPADA_BBOX = (19.048, 72.998, 19.072, 73.022)

DRIVABLE = ("motorway|motorway_link|trunk|trunk_link|primary|primary_link|secondary|"
            "secondary_link|tertiary|tertiary_link|unclassified|residential|living_street|"
            "service|road")

ROADS_FILE = "osm_roads.json"
POIS_FILE = "osm_pois.json"


def _bbox(b: tuple[float, float, float, float]) -> str:
    return ",".join(f"{x:.5f}" for x in b)


def roads_query(bbox=config.BBOX) -> str:
    return f"""[out:json][timeout:180];
way["highway"~"^({DRIVABLE})$"]["area"!="yes"]["access"!~"^(no|private)$"]
   ["service"!~"^(drive-through|emergency_access)$"]({_bbox(bbox)});
(._;>;);
out body;"""


def pois_query(bbox=SANPADA_BBOX, wide=config.BBOX) -> str:
    b = _bbox(bbox)
    return f"""[out:json][timeout:180];
(
  nwr["railway"="station"]({b});
  nwr["amenity"~"^(marketplace|hospital|clinic|doctors|school|college|kindergarten|place_of_worship|bus_station|police|post_office|community_centre|townhall|library|fuel|restaurant|food_court|fast_food|bank|cinema)$"]["name"]({b});
  nwr["shop"~"^(supermarket|mall|department_store|convenience|greengrocer|butcher|seafood|bakery)$"]["name"]({b});
  nwr["leisure"~"^(park|playground|garden|sports_centre|stadium|pitch)$"]["name"]({b});
  nwr["building"~"^(apartments|residential|house)$"]["name"]({b});
  nwr["landuse"="residential"]["name"]({b});
  nwr["place"~"^(suburb|neighbourhood|quarter|locality)$"]({b});
  nwr["landuse"="landfill"]({_bbox(wide)});
);
out center tags;"""


def overpass(query: str, retries: int = 2, timeout: int = 200) -> dict:
    data = urllib.parse.urlencode({"data": query}).encode()
    last: Exception | None = None
    for attempt in range(retries + 1):
        for url in OVERPASS_URLS:
            req = urllib.request.Request(url, data=data, headers={
                "User-Agent": USER_AGENT, "Accept": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                payload.setdefault("_source", url)
                return payload
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last = exc
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Overpass download failed: {last}")


def download(data_dir: Path | None = None, force: bool = False) -> dict[str, Path]:
    """Fetch roads and landmarks (skips files already downloaded unless force)."""
    data_dir = Path(data_dir or config.DATA_DIR)
    data_dir.mkdir(parents=True, exist_ok=True)
    out = {}
    for name, query in ((ROADS_FILE, roads_query()), (POIS_FILE, pois_query())):
        path = data_dir / name
        if force or not path.exists():
            payload = overpass(query)
            payload["_downloaded_at"] = int(time.time())
            payload["_query"] = query
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(path)
        out[name] = path
    return out


def load(name: str, data_dir: Path | None = None) -> dict:
    path = Path(data_dir or config.DATA_DIR) / name
    return json.loads(path.read_text(encoding="utf-8"))


def describe(payload: dict) -> dict:
    """Provenance line for the UI and README."""
    return {
        "osm_base": payload.get("osm3s", {}).get("timestamp_osm_base"),
        "downloaded_at": payload.get("_downloaded_at"),
        "source": payload.get("_source"),
        "elements": len(payload.get("elements", [])),
        "licence": "Data (c) OpenStreetMap contributors, ODbL 1.0",
    }


if __name__ == "__main__":
    paths = download(force=True)
    for name, p in paths.items():
        print(name, describe(load(name)))
