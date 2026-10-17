"""Real Indian data used to calibrate the forecaster (downloaded fresh, with provenance).

1. BBMP grievances (Bruhat Bengaluru Mahanagara Palike, 2020-2025), published by OpenCity
   (data.opencity.in, public domain). Only the "Solid Waste (Garbage) Related" complaints are fetched,
   through the CKAN datastore API: time, ward and sub-category. No names or phone numbers are stored
   (the staff name / remarks columns are not requested).
2. Hourly rain from the Open-Meteo historical weather API (ERA5 reanalysis, CC BY 4.0) for Sanpada,
   Navi Mumbai and for Bengaluru.

What they are used for (wastewise.india_calib): measured Indian weekday, festival and rain effects on
garbage problems, a ward-level complaint-risk model for Bengaluru tested on later years, and real
Sanpada rainfall for the synthetic history. (Rain and days-since-festival were also tried as forecaster
inputs and gave no gain, so they are not used.) No Sanpada fill-level data exists in them.
"""
from __future__ import annotations

import csv
import hashlib
import json
import time
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

from . import config

DIR_NAME = "india"
USER_AGENT = "WasteWise/0.1 (IBM Datathon 2026; forecaster calibration)"
BBMP_PACKAGE = "bbmp-grievances-data"
CKAN = "https://data.opencity.in/api/3/action/"
GARBAGE = "Solid Waste (Garbage) Related"
FIELDS = ["Complaint ID", "Category", "Sub Category", "Grievance Date", "Ward Name", "Grievance Status"]
PLACES = {"sanpada": (19.0590, 73.0120), "bengaluru": (12.9716, 77.5946)}
OPEN_METEO = "https://archive-api.open-meteo.com/v1/archive"


def data_dir() -> Path:
    d = config.DATA_DIR / DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _get(url: str, timeout: int = 180) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception:
            if attempt == 3:
                raise
            time.sleep(3 * (attempt + 1))
    raise RuntimeError("unreachable")


def _write_prov(name: str, prov: dict) -> None:
    p = data_dir() / "provenance.json"
    allp = json.loads(p.read_text()) if p.exists() else {}
    allp[name] = prov
    p.write_text(json.dumps(allp, indent=2))


def provenance() -> dict:
    p = data_dir() / "provenance.json"
    return json.loads(p.read_text()) if p.exists() else {}


def download_bbmp(force: bool = False, page: int = 10000) -> Path:
    path = data_dir() / "bbmp_garbage_complaints.csv"
    if path.exists() and not force:
        return path
    meta = json.loads(_get(CKAN + "package_show?id=" + BBMP_PACKAGE))["result"]
    rows: list[list[str]] = []
    years = []
    for res in sorted(meta["resources"], key=lambda r: r["name"]):
        offset = 0
        while True:
            q = urllib.parse.urlencode({"resource_id": res["id"], "limit": page, "offset": offset,
                                        "filters": json.dumps({"Category": GARBAGE}), "fields": ",".join(FIELDS),
                                        "sort": "_id asc"})
            r = json.loads(_get(CKAN + "datastore_search?" + q))["result"]
            recs = r["records"]
            rows += [[str(x.get(f) or "") for f in FIELDS] for x in recs]
            offset += len(recs)
            if not recs or offset >= r["total"]:
                break
        years.append({"resource": res["name"], "resource_id": res["id"], "rows": offset})
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(FIELDS)
        w.writerows(rows)
    tmp.replace(path)
    blob = path.read_bytes()
    _write_prov("bbmp", {"dataset": meta["title"], "publisher": "BBMP via OpenCity (data.opencity.in)",
                         "licence": meta.get("license_title"), "page": f"https://data.opencity.in/dataset/{BBMP_PACKAGE}",
                         "filter": f"Category = {GARBAGE}", "fields": FIELDS, "resources": years,
                         "downloaded_at": int(time.time()), "rows": len(rows), "bytes": len(blob),
                         "sha256": hashlib.sha256(blob).hexdigest()})
    return path


def download_rain(place: str, start: date, end: date, force: bool = False) -> Path:
    path = data_dir() / f"rain_{place}.csv"
    if path.exists() and not force:
        return path
    lat, lon = PLACES[place]
    q = urllib.parse.urlencode({"latitude": lat, "longitude": lon, "start_date": start.isoformat(),
                                "end_date": end.isoformat(), "hourly": "precipitation", "timezone": "Asia/Kolkata"})
    blob = _get(f"{OPEN_METEO}?{q}")
    d = json.loads(blob)
    times, mm = d["hourly"]["time"], d["hourly"]["precipitation"]
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["time_ist", "rain_mm"])
        w.writerows([t, "" if v is None else v] for t, v in zip(times, mm))
    tmp.replace(path)
    _write_prov(f"rain_{place}", {"dataset": "Open-Meteo historical weather API (ERA5 reanalysis)",
                                  "licence": "CC BY 4.0 (Open-Meteo); ERA5 by Copernicus/ECMWF",
                                  "page": "https://open-meteo.com/en/docs/historical-weather-api",
                                  "query": {"latitude": lat, "longitude": lon, "start": start.isoformat(),
                                            "end": end.isoformat(), "hourly": "precipitation"},
                                  "downloaded_at": int(time.time()), "hours": len(times),
                                  "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    return path


def rain_hourly(place: str) -> dict[str, float]:
    """'YYYY-MM-DDTHH:00' (IST) -> mm of rain in that hour (missing hours left out)."""
    path = data_dir() / f"rain_{place}.csv"
    if not path.exists():
        return {}
    out = {}
    with path.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["rain_mm"] != "":
                out[r["time_ist"]] = float(r["rain_mm"])
    return out


def rain_daily(place: str) -> dict[date, float]:
    out: dict[date, float] = {}
    for t, v in rain_hourly(place).items():
        d = date.fromisoformat(t[:10])
        out[d] = out.get(d, 0.0) + v
    return out


def download_all(force: bool = False) -> dict:
    today = date.today()
    end = date.fromordinal(today.toordinal() - 2)       # the archive lags a couple of days
    p = {"bbmp": str(download_bbmp(force))}
    for place in PLACES:
        p[f"rain_{place}"] = str(download_rain(place, date(2020, 1, 1), end, force))
    return p
