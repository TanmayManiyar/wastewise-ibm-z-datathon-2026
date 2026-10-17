#!/usr/bin/env bash
# Start / stop / check the WasteWise background services (web app and REST API).
#   scripts/services.sh start|stop|restart|status
# Environment: WW_HOST (default 127.0.0.1), WW_APP_PORT (8501), WW_API_PORT (8600).
# PID files and logs live in ~/.wastewise/run.
set -u
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN="${HOME}/.wastewise/run"
HOST="${WW_HOST:-127.0.0.1}"
APP_PORT="${WW_APP_PORT:-8501}"
API_PORT="${WW_API_PORT:-8600}"
PY="$APP_DIR/.venv/bin/python"
mkdir -p "$RUN"

start_one() {  # name, command...
    local name=$1; shift
    if [ -f "$RUN/$name.pid" ] && kill -0 "$(cat "$RUN/$name.pid")" 2>/dev/null; then
        echo "$name already running (pid $(cat "$RUN/$name.pid"))"; return
    fi
    cd "$APP_DIR"
    nohup "$@" >> "$RUN/$name.log" 2>&1 < /dev/null &
    echo $! > "$RUN/$name.pid"
    echo "$name started (pid $(cat "$RUN/$name.pid"))"
}

stop_one() {
    local name=$1
    if [ -f "$RUN/$name.pid" ]; then
        kill "$(cat "$RUN/$name.pid")" 2>/dev/null && echo "$name stopped" || echo "$name was not running"
        rm -f "$RUN/$name.pid"
    else
        echo "$name: no pid file"
    fi
}

status() {
    for n in app api; do
        if [ -f "$RUN/$n.pid" ] && kill -0 "$(cat "$RUN/$n.pid")" 2>/dev/null; then
            echo "$n: running (pid $(cat "$RUN/$n.pid"))"
        else
            echo "$n: stopped"
        fi
    done
    curl -s -o /dev/null -w "app http %{http_code}\n" "http://127.0.0.1:$APP_PORT/healthz" || true
    curl -s "http://127.0.0.1:$API_PORT/api/health" && echo || true
}

case "${1:-status}" in
    start)
        start_one app "$PY" -m streamlit run app/main.py --server.address "$HOST" --server.port "$APP_PORT"
        start_one api "$PY" -m wastewise.api --host "$HOST" --port "$API_PORT"
        ;;
    stop) stop_one app; stop_one api ;;
    restart) stop_one app; stop_one api; sleep 2; bash "$0" start ;;
    status) status ;;
    *) echo "usage: $0 start|stop|restart|status"; exit 2 ;;
esac
