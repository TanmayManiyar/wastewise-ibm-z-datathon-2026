#!/usr/bin/env bash
# Build Apache Arrow C++ and pyarrow from source on s390x (no PyPI wheel exists).
# Streamlit needs pyarrow for table serialisation; only the IPC, compute, CSV,
# JSON and filesystem modules are built (no Parquet / Flight / Gandiva).
# Arrow supports big-endian platforms officially.
set -euo pipefail

ARROW_VERSION=${ARROW_VERSION:-24.0.0}
APP_DIR=${APP_DIR:-$HOME/wastewise}
VENV="$APP_DIR/.venv"
BUILD_DIR=${BUILD_DIR:-$HOME/build}
JOBS=${JOBS:-$(nproc)}
PREFIX=${ARROW_PREFIX:-/usr/local}

mkdir -p "$BUILD_DIR" && cd "$BUILD_DIR"
TARBALL="apache-arrow-$ARROW_VERSION.tar.gz"
URL="https://archive.apache.org/dist/arrow/arrow-$ARROW_VERSION/$TARBALL"
[ -f "$TARBALL" ] || curl -fsSL -o "$TARBALL" "$URL"
curl -fsSL -o "$TARBALL.sha512" "$URL.sha512"
sha512sum -c "$TARBALL.sha512"
[ -d "apache-arrow-$ARROW_VERSION" ] || tar xzf "$TARBALL"
SRC="$BUILD_DIR/apache-arrow-$ARROW_VERSION"

echo "== Arrow C++ $ARROW_VERSION ($(date))"
cmake -S "$SRC/cpp" -B "$BUILD_DIR/arrow-cpp-build" -G Ninja \
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$PREFIX" -DCMAKE_INSTALL_LIBDIR=lib \
    -DARROW_DEPENDENCY_SOURCE=AUTO \
    -DARROW_COMPUTE=ON -DARROW_CSV=ON -DARROW_JSON=ON -DARROW_FILESYSTEM=ON -DARROW_IPC=ON \
    -DARROW_PARQUET=OFF -DARROW_DATASET=OFF -DARROW_FLIGHT=OFF -DARROW_GANDIVA=OFF -DARROW_ACERO=OFF \
    -DARROW_JEMALLOC=OFF -DARROW_MIMALLOC=OFF \
    -DARROW_WITH_RE2=ON -DARROW_WITH_UTF8PROC=ON \
    -DARROW_BUILD_TESTS=OFF -DARROW_BUILD_BENCHMARKS=OFF
cmake --build "$BUILD_DIR/arrow-cpp-build" -j "$JOBS"
sudo cmake --install "$BUILD_DIR/arrow-cpp-build"
sudo ldconfig

echo "== pyarrow $ARROW_VERSION ($(date))"
"$VENV/bin/pip" install -q "cython>=3.1" scikit-build-core "setuptools_scm[toml]>=8"
export CMAKE_PREFIX_PATH="$PREFIX" ARROW_HOME="$PREFIX"
export SETUPTOOLS_SCM_PRETEND_VERSION="$ARROW_VERSION"
CMAKE_BUILD_PARALLEL_LEVEL="$JOBS" "$VENV/bin/pip" install --no-build-isolation -v "$SRC/python" 2>&1 | tail -20
"$VENV/bin/python" -c "import pyarrow as pa, sys; print('pyarrow', pa.__version__, sys.byteorder); print(pa.table({'a': [1, 2]}).to_pandas())"
echo "== done ($(date))"
