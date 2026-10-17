"""Proof of service (GPS + photo), truck positions for re-plans, and route-change notices."""
import io

import pytest

from conftest import NOW_TS
from wastewise import config, db, geo, planner, progress, proof, roads


def _approved(conn, now_ts=NOW_TS):
    p = planner.generate(conn, "op", now_ts=now_ts, seconds=2.0)
    pid = planner.save(conn, p, "op")
    planner.approve(conn, pid, "supervisor")
    return planner.get(conn, pid)


def _offset(lat, lon, north_m):
    return lat + north_m / 111_195.0, lon


def test_gps_tap_is_checked_against_the_bin(conn):
    plan = _approved(conn)
    route = next(r for r in plan["routes"] if r["stops"])
    stops = [s for s in route["stops"] if s["kind"] == "bin"]
    b0, b1, b2 = stops[:3]
    lat, lon = _offset(b0["lat"], b0["lon"], 35)
    progress.record_crew(conn, plan, route["vehicle_id"], "collected", "crew1", b0["bin_id"], 75.0,
                         ts=b0["arrive_ts"], gps={"lat": lat, "lon": lon, "acc": 12, "ts": b0["arrive_ts"]})
    lat, lon = _offset(b1["lat"], b1["lon"], 900)
    progress.record_crew(conn, plan, route["vehicle_id"], "collected", "crew1", b1["bin_id"], 50.0,
                         ts=b1["arrive_ts"], gps={"lat": lat, "lon": lon, "acc": 15, "ts": b1["arrive_ts"]})
    progress.record_crew(conn, plan, route["vehicle_id"], "could_not_reach", "crew1", b2["bin_id"], None,
                         ts=b2["arrive_ts"])
    rows = {r["bin_id"]: r for r in progress.status_rows(conn, plan["plan_date"], b2["arrive_ts"] + 60)}
    v0 = proof.verdict({"kind": "collected", "dist_m": rows[b0["bin_id"]]["dist_m"]})
    v1 = proof.verdict({"kind": "collected", "dist_m": rows[b1["bin_id"]]["dist_m"]})
    v2 = proof.verdict({"kind": "could_not_reach", "dist_m": rows[b2["bin_id"]]["dist_m"]})
    assert v0 == ("green", "verified, 35 m from the bin")
    assert v1[0] == "red" and v1[1] == "900 m away"
    assert v2 == ("grey", "no location")


def test_old_or_missing_fix_is_not_used():
    fix = {"lat": 19.06, "lon": 73.01, "acc": 10}
    assert proof.usable_fix(fix, 1000, now_ts=1000 + 60)["lat"] == 19.06
    assert proof.usable_fix(fix, 1000, now_ts=1000 + proof.FIX_MAX_AGE_S + 1) is None
    assert proof.usable_fix({"error": "denied"}, 1000, 1001) is None
    assert proof.usable_fix(fix, None, 1001) is None


def test_photos_are_shrunk_and_stored(tmp_path, monkeypatch):
    from PIL import Image
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    buf = io.BytesIO()
    Image.new("RGB", (4000, 3000), (120, 160, 90)).save(buf, "JPEG")
    rel = proof.save_photo(buf.getvalue(), "2026-10-17")
    data = proof.photo_bytes(rel)
    img = Image.open(io.BytesIO(data))
    assert max(img.size) == proof.PHOTO_MAX_PX and rel.startswith("2026-10-17/")
    with pytest.raises(ValueError):
        proof.save_photo(b"not a photo", "2026-10-17")
    assert proof.photo_bytes("../../etc/passwd") is None


def test_replan_starts_truck_from_its_gps_position(conn):
    day_start, _ = planner.shift_window(db.get_settings(conn), planner.plan_day(conn, NOW_TS))
    plan = _approved(conn)
    route = next(r for r in plan["routes"] if len([s for s in r["stops"] if s["kind"] == "bin"]) > 6)
    vid = route["vehicle_id"]
    far = [s for s in route["stops"] if s["kind"] == "bin"][-1]          # truck is already near its last stop
    now = day_start + 3600
    lat, lon = _offset(far["lat"], far["lon"], 20)
    progress.record_position(conn, vid, {"lat": lat, "lon": lon, "acc": 10, "ts": now - 60}, "crew1", "Ramesh")
    with db.tx(conn):
        conn.execute("UPDATE events SET ts=? WHERE kind='position'", (now - 60,))
    assert proof.last_positions(conn, now)[vid]["lat"] == pytest.approx(lat)
    net = roads.network_for(conn)
    notes = {}
    starts = planner.replan_starts(conn, plan, net, now, {b["id"]: b for b in db.bins(conn)}, notes)
    assert net.stop_ids[starts[vid][0]] == far["bin_id"]
    assert "GPS position" in notes[vid]
    rp = planner.generate(conn, "op", kind="replan", now_ts=now, seconds=2.0, replan_of=plan)
    assert rp["replan_of_id"] == plan["id"] and "GPS" in rp["start_notes"][vid]
    r2 = next(r for r in rp["routes"] if r["vehicle_id"] == vid)
    assert r2["start_stop"] == far["bin_id"]


def test_route_changes_list_added_and_removed_stops(conn):
    plan = _approved(conn)
    vid = next(r["vehicle_id"] for r in plan["routes"] if r["stops"])
    new = {**plan, "routes": [dict(r) for r in plan["routes"]]}
    r = next(x for x in new["routes"] if x["vehicle_id"] == vid)
    bins_ = [s for s in r["stops"] if s["kind"] == "bin"]
    extra = dict(bins_[0], bin_id="SNP-999", landmark="New place")
    r["stops"] = [extra] + [s for s in r["stops"] if s is not bins_[1]]
    ch = progress.route_changes(conn, plan, new, vid)
    assert [x["landmark"] for x in ch["added"]] == ["New place"]
    assert [x["bin_id"] for x in ch["removed"]] == [bins_[1]["bin_id"]]


def test_route_stop_coordinates_are_the_real_places(conn):
    plan = _approved(conn)
    places = db.places(conn)
    for r in plan["routes"]:
        for s in r["stops"]:
            if s["kind"] == "landfill":
                assert geo.haversine_m(s["lat"], s["lon"], places["landfill"]["lat"], places["landfill"]["lon"]) < 500
            if s["kind"] == "depot":
                assert geo.haversine_m(s["lat"], s["lon"], places["depot"]["lat"], places["depot"]["lon"]) < 500
