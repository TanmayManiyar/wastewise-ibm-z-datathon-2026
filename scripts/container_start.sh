#!/usr/bin/env bash
# Container entry point: first-run initialisation, then the REST API in the background and the web app.
set -euo pipefail
cd /app
if [ ! -f "$WASTEWISE_DATA/wastewise.db" ]; then
    echo "First start: downloading OpenStreetMap data and creating the demonstration database ..."
    python -m wastewise.cli init
    python scripts/train_models.py || echo "Forecaster training skipped (planner falls back to learned profiles)"
fi
python -m wastewise.api --host 0.0.0.0 --port 8600 &
exec python -m streamlit run app/main.py --server.address 0.0.0.0 --server.port 8501
