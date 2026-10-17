"""SQLite storage.

SQLite runs in WAL mode so many Streamlit sessions and the REST API can read
while one writer commits. Every connection sets a busy timeout so concurrent
writers wait instead of failing. Timestamps are stored as integer Unix
seconds (UTC); the UI converts them to India Standard Time.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL            -- JSON
);
CREATE TABLE IF NOT EXISTS bins (
    id TEXT PRIMARY KEY,           -- e.g. SNP-001
    landmark TEXT NOT NULL,
    street TEXT,
    site_type TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    capacity_l INTEGER NOT NULL,
    priority INTEGER NOT NULL DEFAULT 2 CHECK (priority BETWEEN 1 AND 3),
    has_sensor INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    collect_every_days INTEGER NOT NULL DEFAULT 1,
    rest_days TEXT NOT NULL DEFAULT '',  -- weekdays without collection: 0 = Monday ... 6 = Sunday
    osm_ref TEXT,                  -- source OSM element of the landmark
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS vehicles (
    id TEXT PRIMARY KEY,           -- e.g. TRK-1
    name TEXT NOT NULL,
    capacity_l INTEGER NOT NULL,
    shift_start TEXT NOT NULL DEFAULT '06:00',
    shift_hours REAL NOT NULL DEFAULT 4,
    color TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS places (
    kind TEXT PRIMARY KEY,         -- depot | landfill
    name TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    unload_min REAL NOT NULL DEFAULT 0,
    note TEXT
);
CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY,
    bin_id TEXT NOT NULL REFERENCES bins(id),
    ts INTEGER NOT NULL,
    fill_pct REAL,                 -- 0..100 (sensor may saturate at 100)
    source TEXT NOT NULL,          -- sensor | supervisor | crew | complaint | api
    synthetic INTEGER NOT NULL DEFAULT 0,
    fault INTEGER NOT NULL DEFAULT 0,  -- synthetic ground truth: 0 none, 1 stuck, 2 drift, 3 spike
    user TEXT,
    note TEXT
);
CREATE INDEX IF NOT EXISTS ix_readings_bin_ts ON readings(bin_id, ts);
CREATE INDEX IF NOT EXISTS ix_readings_ts ON readings(ts);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ts INTEGER NOT NULL,
    bin_id TEXT REFERENCES bins(id),
    vehicle_id TEXT,
    plan_id INTEGER,
    kind TEXT NOT NULL,            -- collected | could_not_reach | bin_missing | landfill_unload | ...
    fill_before REAL,              -- crew estimate of how full it was
    synthetic INTEGER NOT NULL DEFAULT 0,
    user TEXT,
    person TEXT,                   -- the crew member who tapped (the account is shared by the truck)
    lat REAL,                      -- phone GPS position at the tap (with the crew's permission)
    lon REAL,
    gps_acc_m REAL,
    dist_m REAL,                   -- distance from the bin (proof of service)
    photo TEXT,                    -- photo file under data/photos
    note TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_bin_ts ON events(bin_id, ts);
CREATE INDEX IF NOT EXISTS ix_events_plan ON events(plan_id);
CREATE TABLE IF NOT EXISTS complaints (
    id INTEGER PRIMARY KEY,
    ts INTEGER NOT NULL,
    text TEXT NOT NULL,
    lang TEXT,
    bin_id TEXT REFERENCES bins(id),
    original_bin_id TEXT,          -- kept when a report is moved to another bin
    problem TEXT,
    status TEXT NOT NULL DEFAULT 'open',   -- new (place not yet confirmed) | open | resolved
    reporter TEXT,
    created_by TEXT,
    resolved_by TEXT,
    resolved_at INTEGER,
    parsed TEXT,                   -- JSON of what the parser understood
    synthetic INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY,
    created_at INTEGER NOT NULL,
    plan_date TEXT NOT NULL,       -- YYYY-MM-DD (IST)
    status TEXT NOT NULL,          -- draft | approved | superseded
    kind TEXT NOT NULL DEFAULT 'daily',  -- daily | replan | whatif
    created_by TEXT,
    approved_by TEXT,
    approved_at INTEGER,
    start_ts INTEGER,              -- when the routes start (shift start or re-plan time)
    data_version INTEGER,
    body TEXT NOT NULL             -- JSON: routes, kpis, verifier, baseline, alerts
);
CREATE INDEX IF NOT EXISTS ix_plans_date ON plans(plan_date, status);
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,            -- admin | supervisor | operator | viewer | crew
    vehicle_id TEXT,               -- crew accounts only
    salt TEXT NOT NULL,
    pw_hash TEXT NOT NULL,
    iterations INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL,
    failed_logins INTEGER NOT NULL DEFAULT 0,
    locked_until INTEGER
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY,
    ts INTEGER NOT NULL,
    user TEXT,
    action TEXT NOT NULL,
    detail TEXT NOT NULL,          -- JSON
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS beats (
    bin_id TEXT PRIMARY KEY,
    vehicle_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS crew_members (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    vehicle_id TEXT,               -- the truck the person usually works on (any truck can be chosen)
    active INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS truck_crew (
    day TEXT NOT NULL,             -- YYYY-MM-DD
    vehicle_id TEXT NOT NULL,
    members TEXT NOT NULL,         -- JSON list of names on the truck that day
    holder TEXT,                   -- who is using the crew phone
    set_by TEXT,
    ts INTEGER NOT NULL,
    PRIMARY KEY (day, vehicle_id)
);
CREATE TABLE IF NOT EXISTS route_ack (
    day TEXT NOT NULL,
    vehicle_id TEXT NOT NULL,
    plan_id INTEGER NOT NULL,      -- the route version the crew has seen
    ts INTEGER NOT NULL,
    person TEXT,
    PRIMARY KEY (day, vehicle_id)
);
CREATE TABLE IF NOT EXISTS synthetic_truth (
    bin_id TEXT NOT NULL,
    ts INTEGER NOT NULL,
    fill_pct REAL NOT NULL,        -- true fill incl. overflow (>100); synthetic evaluation only
    PRIMARY KEY (bin_id, ts)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS models (
    id INTEGER PRIMARY KEY,
    created_at INTEGER NOT NULL,
    kind TEXT NOT NULL,            -- forecaster | anomaly
    name TEXT NOT NULL,
    path TEXT,
    metrics TEXT NOT NULL,         -- JSON
    promoted INTEGER NOT NULL DEFAULT 0
);
"""


_INITIALISED: set[str] = set()


def now() -> int:
    return int(time.time())


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(path or config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    if str(path) not in _INITIALISED:
        init(conn)   # schema changes are additive (CREATE ... IF NOT EXISTS)
        _INITIALISED.add(str(path))
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('data_version', '0')")
    conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '1')")
    # additive migrations for databases created by an older version
    if "rest_days" not in _columns(conn, "bins"):
        conn.execute("ALTER TABLE bins ADD COLUMN rest_days TEXT NOT NULL DEFAULT ''")
    cols = _columns(conn, "events")
    for col, typ in (("person", "TEXT"), ("lat", "REAL"), ("lon", "REAL"), ("gps_acc_m", "REAL"),
                     ("dist_m", "REAL"), ("photo", "TEXT")):
        if col not in cols:
            conn.execute(f"ALTER TABLE events ADD COLUMN {col} {typ}")
    migrate_demo_register(conn)


def migrate_demo_register(conn: sqlite3.Connection) -> None:
    """Demonstration register, revision 2: realistic collection days. Schools are not collected on
    Sundays and the public office every second day (parks and playgrounds already were). Only applied
    to the demonstration database, and only to bins still on the old default."""
    if conn.execute("SELECT value FROM meta WHERE key='demo_register_rev'").fetchone():
        return
    if not conn.execute("SELECT 1 FROM meta WHERE key='synthetic_info'").fetchone():
        return
    conn.execute("UPDATE bins SET rest_days='6' WHERE site_type='school' AND rest_days=''")
    conn.execute("UPDATE bins SET collect_every_days=2 WHERE site_type='public_office' AND collect_every_days=1")
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('demo_register_rev', '2')")
    bump_version(conn)


def rest_days(b: dict) -> set[int]:
    return {int(x) for x in str(b.get("rest_days") or "").split(",") if x.strip().isdigit()}


@contextmanager
def tx(conn: sqlite3.Connection, immediate: bool = True) -> Iterator[sqlite3.Connection]:
    """Explicit transaction. IMMEDIATE takes the write lock up front."""
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def bump_version(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'data_version'")


def data_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = 'data_version'").fetchone()
    return int(row[0]) if row else 0


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


# ---------------------------------------------------------------- settings
DEFAULT_SETTINGS: dict[str, Any] = {
    "shift_start": "06:00",
    "shift_hours": 4.0,
    "landfill_unload_min": 20.0,
    "service_min_base": 2.5,           # minutes to position, lift and empty a bin
    "service_min_per_1000l": 1.5,
    "late_threshold_min": 30,
    "collect_threshold_pct": 70.0,     # optional (not-due) bins are added above this predicted fill
    "full_pct": 100.0,
    "speeds_kmh": {                    # typical loaded truck speeds per OSM road class
        "motorway": 40, "trunk": 35, "primary": 28, "secondary": 24, "tertiary": 20,
        "unclassified": 16, "residential": 14, "living_street": 8, "service": 10,
        "road": 14, "link": 22,
    },
    "fuel_l_per_km": 0.35,             # diesel refuse truck, demonstration figure
    "co2_kg_per_l_diesel": 2.68,
    "beat_penalty_min": 12.0,          # cost of serving a bin from another truck's beat
    "optimiser_seconds": 8.0,
    "telum_inference": "auto",         # auto | cpu | accelerator
    "map_tiles": "osm",                # osm (browser loads OpenStreetMap tiles) | offline
    "llm_enabled": True,
    "llm_url": "http://127.0.0.1:8080",  # local llama.cpp server running IBM Granite
    "demo_feed": "auto",               # auto = on in demonstration mode | True | False
}


def get_settings(conn: sqlite3.Connection) -> dict[str, Any]:
    out = json.loads(json.dumps(DEFAULT_SETTINGS))
    for row in conn.execute("SELECT key, value FROM settings"):
        out[row["key"]] = json.loads(row["value"])
    return out


def set_setting(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, json.dumps(value)))


# ---------------------------------------------------------------- helpers
def rows(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, tuple(params))]


def one(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> dict | None:
    r = conn.execute(sql, tuple(params)).fetchone()
    return dict(r) if r else None


def bins(conn: sqlite3.Connection, active_only: bool = True) -> list[dict]:
    sql = "SELECT * FROM bins" + (" WHERE active = 1" if active_only else "") + " ORDER BY id"
    return rows(conn, sql)


def vehicles(conn: sqlite3.Connection, active_only: bool = True) -> list[dict]:
    sql = "SELECT * FROM vehicles" + (" WHERE active = 1" if active_only else "") + " ORDER BY id"
    return rows(conn, sql)


def places(conn: sqlite3.Connection) -> dict[str, dict]:
    return {r["kind"]: r for r in rows(conn, "SELECT * FROM places")}


def add_reading(conn: sqlite3.Connection, bin_id: str, fill_pct: float | None, source: str,
                ts: int | None = None, user: str | None = None, note: str | None = None,
                synthetic: bool = False) -> int:
    if fill_pct is not None:
        fill_pct = max(0.0, min(100.0, float(fill_pct)))
    cur = conn.execute(
        "INSERT INTO readings(bin_id, ts, fill_pct, source, synthetic, user, note) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (bin_id, ts or now(), fill_pct, source, int(synthetic), user, note))
    bump_version(conn)
    return int(cur.lastrowid)


def add_event(conn: sqlite3.Connection, kind: str, bin_id: str | None = None,
              vehicle_id: str | None = None, plan_id: int | None = None,
              fill_before: float | None = None, ts: int | None = None,
              user: str | None = None, note: str | None = None, person: str | None = None,
              gps: dict | None = None, dist_m: float | None = None, photo: str | None = None) -> int:
    gps = gps or {}
    cur = conn.execute(
        "INSERT INTO events(ts, bin_id, vehicle_id, plan_id, kind, fill_before, synthetic, user, note, person,"
        " lat, lon, gps_acc_m, dist_m, photo) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)",
        (ts or now(), bin_id, vehicle_id, plan_id, kind, fill_before, user, note, person,
         gps.get("lat"), gps.get("lon"), gps.get("acc"), dist_m, photo))
    bump_version(conn)
    return int(cur.lastrowid)
