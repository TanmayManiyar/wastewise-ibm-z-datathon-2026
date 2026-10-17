"""On the server, build today's plan map exactly as the app does and decode its route coordinates the
way a (little-endian) browser does. Run on IBM LinuxONE to prove the big-endian fix works:
    python scripts/check_browser_encoding.py
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "app")]

from ui import compat  # noqa: E402
from ui import maps  # noqa: E402
from wastewise import db, planner  # noqa: E402


def decode(v):
    if isinstance(v, dict) and "bdata" in v:
        return np.frombuffer(base64.b64decode(v["bdata"]), dtype="<" + v["dtype"])  # browser = little-endian
    return np.array(v, dtype=float)


def main() -> None:
    print("server byte order:", sys.byteorder, "| plotly patched:", compat.patch_plotly())
    c = db.connect()
    p = planner.current(c, planner.plan_day(c))
    if p is None:
        sys.exit("no plan for the day - generate one first")
    fig = maps.plan_map(p, db.places(c), db.bins(c))
    sent = json.loads(fig.to_json())
    ok = True
    for tr in sent["data"]:
        if tr.get("mode") == "lines" and tr.get("name", "").startswith("Truck"):
            lat, lon = decode(tr["lat"]), decode(tr["lon"])
            good = bool(np.all((lat > 18.9) & (lat < 19.2) & (lon > 72.9) & (lon < 73.2)))
            ok &= good
            print(f"{tr['name']:16s} {len(lat):5d} points  lat {lat.min():.4f}..{lat.max():.4f}  "
                  f"lon {lon.min():.4f}..{lon.max():.4f}  {'OK' if good else 'GARBLED'}")
    print("RESULT:", "route lines reach the browser correctly" if ok else "FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
