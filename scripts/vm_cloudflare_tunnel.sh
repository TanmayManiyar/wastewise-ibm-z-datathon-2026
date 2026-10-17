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
    command -v go > /dev/null || sudo apt-get install -y -qq golang-go
    [ -d "$BUILD_DIR/cloudflared" ] || git clone --depth 1 --branch "$VERSION" https://github.com/cloudflare/cloudflared.git "$BUILD_DIR/cloudflared"
    (cd "$BUILD_DIR/cloudflared" && GOTOOLCHAIN=auto go build -o "$BIN" \
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
