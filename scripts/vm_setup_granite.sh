#!/usr/bin/env bash
# Run IBM Granite locally on the LinuxONE VM with llama.cpp (no external service).
#   bash scripts/vm_setup_granite.sh /path/to/granite-model.gguf
# llama.cpp supports s390x (big-endian). A GGUF file must be big-endian on s390x: if the given
# file is little-endian, a big-endian copy is made with llama.cpp's gguf_convert_endian.py.
# The server listens on 127.0.0.1:8080 only; WasteWise talks to it from the same machine.
set -euo pipefail
MODEL=${1:?usage: vm_setup_granite.sh MODEL.gguf}
BUILD_DIR=${BUILD_DIR:-$HOME/build}
LLAMA_REF=${LLAMA_REF:-master}
RUN="$HOME/.wastewise/run"
mkdir -p "$BUILD_DIR" "$RUN"

if [ ! -x "$BUILD_DIR/llama.cpp/build/bin/llama-server" ]; then
    git -C "$BUILD_DIR" clone --depth 1 --branch "$LLAMA_REF" https://github.com/ggml-org/llama.cpp.git 2>/dev/null \
        || git -C "$BUILD_DIR/llama.cpp" pull
    cmake -S "$BUILD_DIR/llama.cpp" -B "$BUILD_DIR/llama.cpp/build" -DCMAKE_BUILD_TYPE=Release -DLLAMA_CURL=OFF
    cmake --build "$BUILD_DIR/llama.cpp/build" -j "$(nproc)" --target llama-server
fi

# GGUF files start with the magic "GGUF" followed by a uint32 version; on a big-endian file the
# version bytes are reversed. Check and convert if needed.
python3 - "$MODEL" <<'EOF' > /tmp/gguf_endian
import struct, sys
b = open(sys.argv[1], "rb").read(8)
assert b[:4] == b"GGUF", "not a GGUF file"
print("little" if struct.unpack("<I", b[4:])[0] < 65536 else "big")
EOF
ENDIAN=$(cat /tmp/gguf_endian)
echo "model byte order: $ENDIAN"
SERVE="$MODEL"
if [ "$(python3 -c 'import sys; print(sys.byteorder)')" = "big" ] && [ "$ENDIAN" = "little" ]; then
    SERVE="${MODEL%.gguf}.be.gguf"
    if [ ! -f "$SERVE" ]; then
        cp "$MODEL" "$SERVE"
        python3 -m venv "$BUILD_DIR/gguf-venv"
        "$BUILD_DIR/gguf-venv/bin/pip" install -q "$BUILD_DIR/llama.cpp/gguf-py"
        "$BUILD_DIR/gguf-venv/bin/python" "$BUILD_DIR/llama.cpp/gguf-py/gguf/scripts/gguf_convert_endian.py" \
            "$SERVE" big --dry-run=false 2>/dev/null \
            || "$BUILD_DIR/gguf-venv/bin/python" "$BUILD_DIR/llama.cpp/gguf-py/gguf/scripts/gguf_convert_endian.py" "$SERVE" big
    fi
fi

if [ -f "$RUN/llm.pid" ] && kill -0 "$(cat "$RUN/llm.pid")" 2>/dev/null; then kill "$(cat "$RUN/llm.pid")"; sleep 2; fi
nohup "$BUILD_DIR/llama.cpp/build/bin/llama-server" -m "$SERVE" --host 127.0.0.1 --port 8080 -c 4096 \
      -t "$(nproc)" --jinja > "$RUN/llm.log" 2>&1 < /dev/null &
echo $! > "$RUN/llm.pid"
sleep 5
curl -s http://127.0.0.1:8080/v1/models && echo
