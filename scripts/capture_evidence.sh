#!/usr/bin/env bash
# Capture reproducible evidence from the machine WasteWise runs on (meant for the LinuxONE VM).
# Writes evidence/<host>-<UTC timestamp>/ with: machine facts, library versions, test results,
# planning timings, model evaluation, and the CPU-vs-Telum benchmark. Nothing is edited by hand.
#   bash scripts/capture_evidence.sh
set -u
cd "$(dirname "$0")/.."
PY=.venv/bin/python
OUT="evidence/$(hostname)-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$OUT"
echo "Writing $OUT"

{
  echo "# captured $(date -u --iso-8601=seconds) on $(hostname)"
  echo "## uname";        uname -a
  echo "## os-release";   cat /etc/os-release
  echo "## lscpu";        lscpu
  echo "## cpuinfo (first lines)"; grep -E "^(vendor_id|features|machine|processors)" /proc/cpuinfo | head -8
  echo "## memory";       free -h
  echo "## git revision"; cat REVISION 2>/dev/null || git rev-parse --short HEAD 2>/dev/null
} > "$OUT/machine.txt" 2>&1

$PY -m wastewise.cli platform > "$OUT/platform.json" 2>&1
$PY - > "$OUT/libraries.txt" 2>&1 <<'EOF'
import importlib, sys
print("python", sys.version.replace("\n", " "))
for m in ("numpy", "scipy", "pandas", "sklearn", "pyarrow", "streamlit", "plotly", "lightgbm", "xgboost", "snapml"):
    try:
        mod = importlib.import_module(m)
        print(f"{m:10s} {getattr(mod, '__version__', '?')}  {getattr(mod, '__file__', '')}")
    except Exception as exc:
        print(f"{m:10s} NOT AVAILABLE ({type(exc).__name__})")
EOF

echo "Running tests ..."
$PY -m pytest -q tests > "$OUT/tests.txt" 2>&1
tail -1 "$OUT/tests.txt"

echo "Timing the planner ..."
$PY - > "$OUT/planner_timing.json" 2>&1 <<'EOF'
import json, time, warnings
warnings.filterwarnings("ignore")
from wastewise import db, planner, roads
c = db.connect()
t = time.perf_counter(); net = roads.build_network(roads.stops_from_db(c), db.get_settings(c)["speeds_kmh"])
t_net = time.perf_counter() - t
now = db.now()
if planner.in_shift(c, now):
    now -= 6 * 3600
t = time.perf_counter(); p = planner.generate(c, "evidence", now_ts=now, seconds=8, seed=1); t_plan = time.perf_counter() - t
print(json.dumps({"label": "planning on SYNTHETIC demonstration data",
                  "road_network": {"nodes": net.graph.n, "edges": net.graph.n_edges(), "stops": len(net.stop_ids),
                                   "build_seconds": round(t_net, 3)},
                  "plan_seconds_total": round(t_plan, 2), "solver": p["solver"], "verifier_ok": p["verifier"]["ok"],
                  "forecast_source": p["forecast_source"], "kpis": p["kpis"], "baseline_routine": p["baseline"]},
                 indent=2, default=str))
EOF

echo "Training and evaluating models ..."
$PY scripts/train_models.py > "$OUT/train_models.log" 2>&1
cp evidence/models_*.json "$OUT/" 2>/dev/null
echo "Benchmarking tree inference (CPU vs Telum) ..."
$PY scripts/benchmark_telum.py > "$OUT/benchmark_telum.log" 2>&1
cp evidence/telum_*.json "$OUT/" 2>/dev/null
$PY scripts/eval_state_risk.py --json "$OUT/state_risk.json" > /dev/null 2>&1
$PY -m wastewise.cli verify-audit > "$OUT/audit_check.json" 2>&1
( cd "$OUT" && sha256sum * > SHA256SUMS )
echo "Done: $OUT"
