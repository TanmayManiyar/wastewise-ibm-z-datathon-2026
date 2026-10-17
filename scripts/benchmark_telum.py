"""Benchmark the forecaster's boosted trees: native library on CPU vs IBM Snap ML on
CPU vs IBM Snap ML on the Telum AI accelerator. Writes evidence/telum_<host>.json.
Only meaningful on IBM z16/z17 (LinuxONE 4/5); elsewhere it reports CPU only.

Usage: python scripts/benchmark_telum.py
"""
from __future__ import annotations

import json
import platform
import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
warnings.filterwarnings("ignore")

from wastewise import db, forecast, telum  # noqa: E402


def main() -> None:
    conn = db.connect()
    cur = forecast.promoted(conn)
    if not cur:
        sys.exit("No promoted forecaster - run scripts/train_models.py first")
    model = forecast._load(cur["path"])
    end = int(conn.execute("SELECT MAX(ts) FROM readings").fetchone()[0])
    X, _, _, _ = forecast.dataset(forecast.sanpada_series(conn, end - 30 * 86400, end))
    res = telum.benchmark(model, Path(cur["path"]), X)
    res["model_id"] = cur["id"]
    print("Sweeping model sizes ...")
    Xs, ys, _, _ = forecast.dataset(forecast.sanpada_series(conn, end - 60 * 86400, end))
    res["size_sweep"] = telum.sweep(Xs, ys)
    for r in res["size_sweep"]:
        print(r)
    res["note"] = ("batch_720 = one planning run (30 sensor bins x 24 hourly leads); "
                   "batch_20000 = bulk scoring")
    ev = ROOT / "evidence"
    ev.mkdir(exist_ok=True)
    path = ev / f"telum_{platform.node()}.json"
    path.write_text(json.dumps(res, indent=2, default=str))
    print(json.dumps(res["engines"], indent=2))
    print("written", path)


if __name__ == "__main__":
    main()
