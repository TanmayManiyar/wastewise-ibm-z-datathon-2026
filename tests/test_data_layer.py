import threading
from datetime import datetime

import pytest

from wastewise import area, audit, auth, calendar_in, config, db, synth
from conftest import NOW_TS


# ---------------------------------------------------------------- database
def test_init_is_idempotent_and_wal(empty_conn):
    db.init(empty_conn)
    assert empty_conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert db.data_version(empty_conn) == 0


def test_settings_roundtrip(empty_conn):
    s = db.get_settings(empty_conn)
    assert s["shift_hours"] == 4.0 and s["landfill_unload_min"] == 20.0
    db.set_setting(empty_conn, "shift_hours", 5)
    assert db.get_settings(empty_conn)["shift_hours"] == 5


def test_reading_clamped_and_version_bumped(conn):
    v0 = db.data_version(conn)
    rid = db.add_reading(conn, "SNP-001", 130, "supervisor", user="supervisor")
    assert db.one(conn, "SELECT fill_pct FROM readings WHERE id=?", (rid,))["fill_pct"] == 100
    assert db.data_version(conn) == v0 + 1


def test_concurrent_writers(conn, tmp_path):
    """Several sessions writing at once must neither fail nor lose rows."""
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    errors = []

    def worker(k):
        c = db.connect(path)
        try:
            for i in range(25):
                with db.tx(c):
                    db.add_reading(c, "SNP-002", float(i), "api", user=f"w{k}")
        except Exception as exc:  # pragma: no cover
            errors.append(exc)
        finally:
            c.close()

    before = db.one(conn, "SELECT COUNT(*) n FROM readings WHERE source='api'")["n"]
    threads = [threading.Thread(target=worker, args=(k,)) for k in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert db.one(conn, "SELECT COUNT(*) n FROM readings WHERE source='api'")["n"] == before + 150


# ---------------------------------------------------------------- area
def test_bin_register(conn):
    bins = db.bins(conn)
    assert len(bins) == 50
    assert len({b["id"] for b in bins}) == 50
    s, w, n, e = area.SERVICE_AREA
    for b in bins:
        assert s - 0.002 <= b["lat"] <= n + 0.002 and w - 0.002 <= b["lon"] <= e + 0.002
        assert 1 <= b["priority"] <= 3 and b["capacity_l"] > 0 and b["collect_every_days"] >= 1
        assert not area.NO_STOP_NAME.search(b["street"] or ""), b
    names = {b["landmark"] for b in bins}
    assert "D-Mart Juinagar" in names
    assert "Sanpada railway station" in names
    types = {b["site_type"] for b in bins}
    assert {"hospital", "school", "park", "temple", "housing", "station"} <= types


def test_fleet_and_places(conn):
    v = db.vehicles(conn)
    assert [x["capacity_l"] for x in v] == [12000, 12000, 6000]
    import colorsys
    for x in v:
        r, g, b = (int(x["color"][i:i + 2], 16) / 255 for i in (1, 3, 5))
        hue = colorsys.rgb_to_hsv(r, g, b)[0] * 360
        # teal ~175, blue ~220, purple ~260; red/orange/amber/green are below 160 or above 330
        assert 165 <= hue <= 300, f"{x['color']} (hue {hue:.0f}) must be blue/purple/teal"
    p = db.places(conn)
    assert p["landfill"]["unload_min"] == 20 and "confirm with NMMC" in p["landfill"]["name"]
    assert "confirm with NMMC" in p["depot"]["name"]


# ---------------------------------------------------------------- synthetic history
def test_synthetic_rows_are_labelled(conn):
    for table in ("readings", "events", "complaints"):
        r = db.one(conn, f"SELECT COUNT(*) n, SUM(synthetic) s FROM {table}")
        assert r["n"] > 0 and r["n"] == r["s"], table
    info = db.get_meta(conn, "synthetic_info")
    assert "SYNTHETIC" in info


def test_sensor_values_in_range_and_faults_labelled(conn):
    r = db.one(conn, "SELECT MIN(fill_pct) lo, MAX(fill_pct) hi, SUM(fault > 0) f, COUNT(*) n"
                     " FROM readings WHERE source='sensor'")
    assert 0 <= r["lo"] and r["hi"] <= 100
    assert 0.01 < r["f"] / r["n"] < 0.15


def test_no_routine_collection_on_plan_day(conn):
    plan_day = synth.plan_day_for(datetime.fromtimestamp(NOW_TS, config.IST), "06:00", 4)
    assert plan_day.isoformat() == "2026-10-17"
    start = int(datetime(2026, 10, 17, tzinfo=config.IST).timestamp())
    assert db.one(conn, "SELECT COUNT(*) n FROM events WHERE ts >= ?", (start,))["n"] == 0


def test_refresh_keeps_manual_records(conn):
    db.add_reading(conn, "SNP-003", 55, "supervisor", user="supervisor", ts=NOW_TS - 100)
    conn.execute("INSERT INTO complaints(ts, text, status, synthetic) VALUES (?, 'manual', 'open', 0)", (NOW_TS,))
    synth.generate(conn, end_ts=NOW_TS + 3600, days=30)
    assert db.one(conn, "SELECT COUNT(*) n FROM readings WHERE synthetic=0")["n"] == 1
    assert db.one(conn, "SELECT COUNT(*) n FROM complaints WHERE synthetic=0")["n"] == 1
    assert len(db.bins(conn)) == 50


def test_generator_is_deterministic(conn):
    a = synth.generate(conn, end_ts=NOW_TS, days=20, seed=7)
    rows_a = conn.execute("SELECT bin_id, ts, fill_pct FROM synthetic_truth ORDER BY bin_id, ts").fetchall()
    b = synth.generate(conn, end_ts=NOW_TS, days=20, seed=7)
    rows_b = conn.execute("SELECT bin_id, ts, fill_pct FROM synthetic_truth ORDER BY bin_id, ts").fetchall()
    assert a["rows"] == b["rows"] and [tuple(x) for x in rows_a] == [tuple(x) for x in rows_b]


def test_festival_multiplier():
    from datetime import date
    assert calendar_in.festival_multiplier(date(2026, 9, 16), "temple") > 1.5
    assert calendar_in.festival_multiplier(date(2026, 8, 1), "temple") == 1.0
    assert calendar_in.is_monsoon(date(2026, 7, 15)) and not calendar_in.is_monsoon(date(2026, 12, 1))


# ---------------------------------------------------------------- security
def test_password_hashing():
    salt, h, it = auth.hash_password("correct horse")
    assert it >= 600_000 and len(salt) == 32
    assert auth.check_password("correct horse", salt, h, it)
    assert not auth.check_password("wrong", salt, h, it)
    assert auth.hash_password("correct horse")[1] != h   # new salt each time


def test_seed_users_and_login(conn, tmp_path):
    path = auth.seed_users(conn, secrets_dir=tmp_path)
    text = path.read_text()
    pw = {line.split()[0]: line.split()[2] for line in text.splitlines() if line and not line.startswith("#")}
    assert set(pw) >= {"admin", "supervisor", "operator", "viewer", "crew1", "crew2", "crew3"}
    # Plaintext passwords are not stored in the database.
    dump = "\n".join(conn.iterdump())
    assert all(p not in dump for p in pw.values())
    u = auth.authenticate(conn, "crew2", pw["crew2"])
    assert u["role"] == "crew" and u["vehicle_id"] == "TRK-2"
    assert auth.authenticate(conn, "crew2", "nope") is None
    assert auth.seed_users(conn, secrets_dir=tmp_path) is None   # only once


def test_lockout_after_failures(conn):
    auth.create_user(conn, "tester", "T", "viewer", password="s3cret-pass")
    for _ in range(auth.MAX_FAILED):
        assert auth.authenticate(conn, "tester", "bad") is None
    assert auth.authenticate(conn, "tester", "s3cret-pass") is None   # locked now


def test_roles():
    assert auth.can("supervisor", "approve_plan") and not auth.can("operator", "approve_plan")
    assert auth.can("crew", "record_progress") and not auth.can("crew", "view_office")
    assert auth.can("admin", "demo_tools") and not auth.can("supervisor", "demo_tools")


def test_api_key_stored_as_hash(conn, tmp_path):
    path = auth.seed_api_key(conn, secrets_dir=tmp_path)
    key = path.read_text().strip()
    assert auth.check_api_key(conn, key) == "default ingestion key"
    assert auth.check_api_key(conn, key + "x") is None
    assert key not in "\n".join(conn.iterdump())


def test_audit_chain_detects_tampering(conn):
    for i in range(5):
        audit.log(conn, "supervisor", "test_action", {"i": i})
    assert audit.verify(conn)["ok"]
    rid = conn.execute("SELECT id FROM audit ORDER BY id LIMIT 1 OFFSET 2").fetchone()[0]
    conn.execute("UPDATE audit SET detail = '{\"i\": 99}' WHERE id = ?", (rid,))
    res = audit.verify(conn)
    assert not res["ok"] and res["first_bad_id"] == rid
