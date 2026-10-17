"""Real-world test data: Mildura Rural City Council smart-bin fill levels.

Dataset "Mildura RRC Smart Bin Fill Levels", published on data.gov.au by
Mildura Rural City Council (Victoria, Australia) under Creative Commons
Attribution 3.0 Australia. Downloaded fresh through the data.gov.au CKAN API;
the provenance (URL, licence, time, SHA-256) is stored next to the file.

Used only to test the forecaster on real sensor data from a different city;
it is never mixed into the Sanpada demonstration data.
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.request
from pathlib import Path

from . import config

PACKAGE = "mildura-rrc-smart-bin-fill-levels"
API = "https://data.gov.au/data/api/3/action/package_show?id=" + PACKAGE
USER_AGENT = "WasteWise/0.1 (IBM Datathon 2026; forecaster validation)"
CSV_NAME = "mildura-bin-sensor-data.csv"


def _get(url: str, timeout: int = 120) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def download(force: bool = False, out_dir: Path | None = None) -> Path:
    out_dir = Path(out_dir or config.MILDURA_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / CSV_NAME
    if path.exists() and not force:
        return path
    meta = json.loads(_get(API))["result"]
    res = next(r for r in meta["resources"] if (r.get("format") or "").upper() == "CSV")
    blob = _get(res["url"])
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(blob)
    tmp.replace(path)
    prov = {"dataset": meta["title"], "publisher": (meta.get("organization") or {}).get("title"),
            "licence": meta.get("license_title"), "page": f"https://data.gov.au/data/dataset/{PACKAGE}",
            "resource_url": res["url"], "resource_last_modified": res.get("last_modified") or res.get("created"),
            "downloaded_at": int(time.time()), "bytes": len(blob), "sha256": hashlib.sha256(blob).hexdigest()}
    (out_dir / "provenance.json").write_text(json.dumps(prov, indent=2))
    return path


def provenance(out_dir: Path | None = None) -> dict | None:
    p = Path(out_dir or config.MILDURA_DIR) / "provenance.json"
    return json.loads(p.read_text()) if p.exists() else None
