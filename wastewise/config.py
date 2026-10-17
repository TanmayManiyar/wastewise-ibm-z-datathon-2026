"""Paths and fixed settings. Everything can be overridden with environment variables."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("WASTEWISE_DATA", ROOT / "data"))
SECRETS_DIR = Path(os.environ.get("WASTEWISE_SECRETS", ROOT / "secrets"))
MODELS_DIR = Path(os.environ.get("WASTEWISE_MODELS", ROOT / "models"))
DB_PATH = Path(os.environ.get("WASTEWISE_DB", DATA_DIR / "wastewise.db"))

OSM_RAW = DATA_DIR / "osm_sanpada.json"
ROADS_CACHE = DATA_DIR / "roads.npz"
MILDURA_DIR = DATA_DIR / "mildura"

# Timezone of the ward office (India Standard Time, no DST).
TZ_NAME = "Asia/Kolkata"

# Bounding box of the study area: Sanpada node plus the roads to the
# Turbhe landfill (south, west, north, east).
BBOX = (19.040, 72.990, 19.090, 73.035)


def ensure_dirs() -> None:
    for d in (DATA_DIR, SECRETS_DIR, MODELS_DIR):
        d.mkdir(parents=True, exist_ok=True)
