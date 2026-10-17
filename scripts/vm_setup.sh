#!/usr/bin/env bash
# Prepare an Ubuntu 24.04 s390x (IBM LinuxONE) VM for WasteWise.
#
# Why this layout: on PyPI, numpy / scipy / pandas / scikit-learn / pyarrow
# have no s390x wheels for CPython 3.12, so
#   * numpy, scipy, pandas, scikit-learn come from Ubuntu's s390x builds (apt),
#   * pyarrow (needed by Streamlit) is built from the Apache Arrow sources
#     (scripts/vm_build_pyarrow.sh),
#   * IBM Snap ML comes from its native s390x wheel,
#   * Streamlit, Plotly and the rest are pure-Python wheels.
# The virtual environment uses --system-site-packages to see the apt builds;
# a constraints file stops pip from replacing them with source builds.
set -euo pipefail

APP_DIR=${APP_DIR:-$HOME/wastewise}
VENV="$APP_DIR/.venv"

export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -qq
sudo apt-get install -y -qq \
    python3.12-venv python3-pip python3-dev build-essential gfortran cmake ninja-build pkg-config \
    python3-numpy python3-scipy python3-pandas python3-sklearn python3-joblib python3-threadpoolctl \
    python3-pil libre2-dev libutf8proc-dev rapidjson-dev libssl-dev sqlite3 curl \
    libzdnn0 libzdnn-dev   # IBM zDNN: lets Snap ML run tree inference on the Telum NNPA

if [ ! -x "$VENV/bin/python" ] || ! grep -q "include-system-site-packages = true" "$VENV/pyvenv.cfg"; then
    rm -rf "$VENV"
    python3 -m venv --system-site-packages "$VENV"
fi
"$VENV/bin/pip" install -q --upgrade pip

# Pin the apt-provided scientific stack so pip never tries to rebuild it.
"$VENV/bin/python" - > "$APP_DIR/constraints-s390x.txt" <<'EOF'
import numpy, scipy, pandas, sklearn, PIL
for name, mod in [("numpy", numpy), ("scipy", scipy), ("pandas", pandas),
                  ("scikit-learn", sklearn), ("pillow", PIL)]:
    print(f"{name}=={mod.__version__}")
EOF
cat "$APP_DIR/constraints-s390x.txt"

if ! "$VENV/bin/python" -c "import pyarrow" 2>/dev/null; then
    bash "$(dirname "$0")/vm_build_pyarrow.sh"
fi

"$VENV/bin/pip" install -q -c "$APP_DIR/constraints-s390x.txt" streamlit plotly snapml pytest
# No s390x wheels: build the gradient-boosting libraries from source (a few minutes each).
"$VENV/bin/pip" install -q -c "$APP_DIR/constraints-s390x.txt" --no-binary xgboost,lightgbm xgboost lightgbm
"$VENV/bin/python" - <<'EOF'
import numpy, scipy, pandas, sklearn, pyarrow, streamlit, plotly, snapml
for m in (numpy, scipy, pandas, sklearn, pyarrow, streamlit, plotly, snapml):
    print(f"{m.__name__:12s} {getattr(m, '__version__', '?')}")
EOF
