#!/usr/bin/env bash
# Public HTTPS link for the WasteWise web app through a Cloudflare quick tunnel.
# The VM only makes an outgoing connection to Cloudflare - no firewall port is opened.
# Cloudflare publishes no s390x binary, so cloudflared is built from the official source release.
#   bash scripts/vm_cloudflare_tunnel.sh            # build if needed, start, print the link
#   bash scripts/vm_cloudflare_tunnel.sh stop
set -euo pipefail
VERSION=${CLOUDFLARED_VERSION:-2026.10.1}
BUILD_DIR=${BUILD_DIR:-$HOME/build}
BIN="$HOME/.local/bin/cloudflared"
RUN="$HOME/.wastewise/run"
PORT=${WW_APP_PORT:-8501}
mkdir -p "$BUILD_DIR" "$RUN" "$(dirname "$BIN")"

if [ "${1:-start}" = "stop" ]; then
    [ -f "$RUN/tunnel.pid" ] && kill "$(cat "$RUN/tunnel.pid")" 2>/dev/null && echo "tunnel stopped"
    rm -f "$RUN/tunnel.pid"
    exit 0
fi

if [ ! -x "$BIN" ]; then
    # Official Go toolchain for linux-s390x (cloudflared needs a newer Go than Ubuntu ships), in $HOME only.
    GO_VERSION=${GO_VERSION:-1.27.2}
    GOROOT_DIR="$HOME/.local/go$GO_VERSION"
    if [ ! -x "$GOROOT_DIR/bin/go" ]; then
        ARCH=$(uname -m); [ "$ARCH" = "x86_64" ] && ARCH=amd64; [ "$ARCH" = "aarch64" ] && ARCH=arm64
        TGZ="go$GO_VERSION.linux-$ARCH.tar.gz"
        curl -fsSL -o "$BUILD_DIR/$TGZ" "https://go.dev/dl/$TGZ"
        SUM=$(curl -fsSL "https://go.dev/dl/?mode=json" | python3 -c "import json,sys; print(next(f['sha256'] for r in json.load(sys.stdin) for f in r['files'] if f['filename']=='$TGZ'))")
        echo "$SUM  $BUILD_DIR/$TGZ" | sha256sum -c -
        mkdir -p "$GOROOT_DIR" && tar -xzf "$BUILD_DIR/$TGZ" -C "$GOROOT_DIR" --strip-components=1
    fi
    [ -d "$BUILD_DIR/cloudflared" ] || git clone --depth 1 --branch "$VERSION" https://github.com/cloudflare/cloudflared.git "$BUILD_DIR/cloudflared"
    (cd "$BUILD_DIR/cloudflared" && GOTOOLCHAIN=local "$GOROOT_DIR/bin/go" build -o "$BIN" \
        -ldflags "-X main.Version=$VERSION" ./cmd/cloudflared)
fi
"$BIN" --version

if [ -f "$RUN/tunnel.pid" ] && kill -0 "$(cat "$RUN/tunnel.pid")" 2>/dev/null; then
    kill "$(cat "$RUN/tunnel.pid")"; sleep 2
fi
: > "$RUN/tunnel.log"
nohup "$BIN" tunnel --no-autoupdate --url "http://127.0.0.1:$PORT" > "$RUN/tunnel.log" 2>&1 < /dev/null &
echo $! > "$RUN/tunnel.pid"
for _ in $(seq 1 30); do
    URL=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$RUN/tunnel.log" | head -1 || true)
    [ -n "$URL" ] && break
    sleep 2
done
echo "PUBLIC LINK: ${URL:-not ready - see $RUN/tunnel.log}"
