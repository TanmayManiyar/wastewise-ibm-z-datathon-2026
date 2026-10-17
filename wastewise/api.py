"""REST ingestion API (Python standard library only - no web framework needed on s390x).

  GET  /api/health                      no key: liveness, database and platform
  POST /api/readings      (X-API-Key)   {"bin_id", "fill_pct", "ts"?} or a list of them
  POST /api/complaints    (X-API-Key)   {"text", "reporter"?} -> parsed; waits for a user to confirm the place
  GET  /api/events?since_id=N (X-API-Key)  collections and crew updates after id N
  GET  /api/plan/latest?date=YYYY-MM-DD (X-API-Key)  the approved plan (routes and stops)

API keys are stored only as SHA-256 hashes. Every write is audited as "api:<key name>".
Run: python -m wastewise.api --host 127.0.0.1 --port 8600
"""
from __future__ import annotations

import argparse
import json
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import __version__, audit, auth, complaints, db, planner, platform_info

MAX_BODY = 64 * 1024
RATE_LIMIT = 120          # requests per minute per key
_local = threading.local()
_hits: dict[str, deque] = defaultdict(deque)


def _conn():
    if not hasattr(_local, "conn"):
        _local.conn = db.connect()
    return _local.conn


class Handler(BaseHTTPRequestHandler):
    server_version = f"WasteWise/{__version__}"

    def log_message(self, fmt, *args):  # keep request logs short and without keys
        pass

    # ----------------------------------------------------------- helpers
    def _send(self, code: int, payload: dict | list) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _key(self) -> str | None:
        name = auth.check_api_key(_conn(), self.headers.get("X-API-Key"))
        if not name:
            self._send(401, {"error": "missing or invalid X-API-Key"})
            return None
        q = _hits[name]
        now = time.time()
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= RATE_LIMIT:
            self._send(429, {"error": "rate limit"})
            return None
        q.append(now)
        return name

    def _json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > MAX_BODY:
            self._send(413 if n > MAX_BODY else 400, {"error": "JSON body required (max 64 KB)"})
            return None
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send(400, {"error": "invalid JSON"})
            return None

    # ----------------------------------------------------------- routes
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/api/health":
            c = _conn()
            p = platform_info.detect()
            return self._send(200, {"status": "ok", "version": __version__, "time": db.now(),
                                    "data_version": db.data_version(c), "bins": len(db.bins(c)),
                                    "platform": {k: p[k] for k in ("arch", "machine_type", "telum", "byteorder")}})
        key = self._key()
        if not key:
            return
        c = _conn()
        if u.path == "/api/events":
            since = int((q.get("since_id") or ["0"])[0])
            rows = db.rows(c, "SELECT id, ts, bin_id, vehicle_id, plan_id, kind, fill_before, synthetic FROM events"
                              " WHERE id > ? ORDER BY id LIMIT 500", (since,))
            return self._send(200, {"events": rows, "next_since_id": rows[-1]["id"] if rows else since})
        if u.path == "/api/plan/latest":
            day = (q.get("date") or [planner.plan_day(c).isoformat()])[0]
            p = planner.current(c, day)
            if not p or p["status"] != "approved":
                return self._send(404, {"error": f"no approved plan for {day}"})
            return self._send(200, {
                "id": p["id"], "date": p["plan_date"], "kind": p["kind"], "approved_at": p["approved_at"],
                "label": p.get("label"),
                "routes": [{"vehicle_id": r["vehicle_id"], "stops": [
                    {k: s.get(k) for k in ("seq", "kind", "stop_id", "arrive_ts", "depart_ts", "landmark",
                                           "expected_fill_pct", "lat", "lon")} for s in r["stops"]]}
                    for r in p["routes"]]})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        if u.path not in ("/api/readings", "/api/complaints"):
            return self._send(404, {"error": "not found"})
        key = self._key()
        if not key:
            return
        data = self._json()
        if data is None:
            return
        c = _conn()
        actor = f"api:{key}"
        if u.path == "/api/readings":
            items = data if isinstance(data, list) else [data]
            if len(items) > 500:
                return self._send(413, {"error": "max 500 readings per request"})
            bins = {b["id"] for b in db.bins(c)}
            ok, errors = [], []
            now = db.now()
            for i, it in enumerate(items):
                try:
                    bid = str(it["bin_id"])
                    fill = float(it["fill_pct"])
                    ts = int(it.get("ts") or now)
                except (KeyError, TypeError, ValueError):
                    errors.append({"index": i, "error": "need bin_id and numeric fill_pct"})
                    continue
                if bid not in bins:
                    errors.append({"index": i, "error": f"unknown bin {bid}"})
                elif not (-5 <= fill <= 150):
                    errors.append({"index": i, "error": "fill_pct out of range"})
                elif ts > now + 300 or ts < now - 30 * 86400:
                    errors.append({"index": i, "error": "timestamp too far in the future or past"})
                else:
                    ok.append((bid, fill, ts))
            if ok:
                with db.tx(c):
                    ids = [db.add_reading(c, b, f, "api", ts=t, user=actor) for b, f, t in ok]
                    audit.log(c, actor, "api_readings", {"count": len(ids), "first_id": ids[0]})
            return self._send(200 if not errors else 207, {"stored": len(ok), "errors": errors})
        text = str(data.get("text", "")).strip()
        if not text or len(text) > 2000:
            return self._send(400, {"error": "text required (max 2000 characters)"})
        und = complaints.understand(text, complaints.PlaceIndex(db.bins(c)))
        cid = complaints.record(c, text, None, und, actor, reporter=str(data.get("reporter") or "")[:100] or None,
                                status="new")
        return self._send(200, {"complaint_id": cid, "status": "new", "understood": und.to_json(),
                                "note": "a control-room user must confirm the place"})


def serve(host: str = "127.0.0.1", port: int = 8600) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    return httpd


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8600)
    a = ap.parse_args()
    httpd = serve(a.host, a.port)
    print(f"WasteWise API on http://{a.host}:{a.port}/api/health")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
