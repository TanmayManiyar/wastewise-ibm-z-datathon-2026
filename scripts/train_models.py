"""Train/evaluate the forecaster, test it on real Mildura data, evaluate fault
detection, and write the results to evidence/models_<host>.json.

Usage: python scripts/train_models.py [--no-mildura]
"""
from __future__ import annotations

import argparse
import json
import platform
import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
warnings.filterwarnings("ignore")

from wastewise import anomaly, db, forecast, mildura, platform_info  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-mildura", action="store_true")
    a = ap.parse_args()
    conn = db.connect()
    out = {"machine": platform_info.detect()}
    print("Training forecaster on the Sanpada (synthetic) history ...")
    out["forecaster"] = forecast.train_and_register(conn, "train_models.py")
    print(json.dumps(out["forecaster"]["decision"]))
    print("Live-style check (forecasts made with data cut at the forecast time) ...")
    cur = forecast.promoted(conn)
    bounds = json.loads(db.get_meta(conn, "forecast_test_window"))
    end = int(conn.execute("SELECT MAX(ts) FROM readings").fetchone()[0])
    out["forecaster_live_check"] = forecast.live_backtest(conn, forecast._load(cur["path"]), cur["metrics"]["best_model"],
                                                          bounds["test_start"], end)
    print(json.dumps({k: v for k, v in out["forecaster_live_check"].items() if k != "by_hours_ahead"}))
    print("Evaluating fault detection against synthetic fault labels ...")
    out["anomaly"] = anomaly.evaluate_synthetic(conn)
    if not a.no_mildura:
        print("Real-world test on Mildura smart-bin data ...")
        san = forecast._load(forecast.promoted(conn)["path"])
        out["mildura"] = forecast.mildura_evaluation(mildura.download(), san)
        out["mildura"]["provenance"] = mildura.provenance()
    ev = ROOT / "evidence"
    ev.mkdir(exist_ok=True)
    path = ev / f"models_{platform.node()}.json"
    path.write_text(json.dumps(out, indent=2, default=str))
    print("written", path)


if __name__ == "__main__":
    main()
