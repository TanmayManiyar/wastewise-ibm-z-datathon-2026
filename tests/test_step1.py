"""Formatting, colour scale, collection days, background plans and the demonstration roll-forward."""
import json
from datetime import date, datetime

from conftest import NOW_TS
from wastewise import config, db, fmt, jobs, live, planner, synth


def test_formatting_is_the_same_everywhere():
    assert fmt.km(70.43) == "70 km" and fmt.km1(4.26) == "4.3 km" and fmt.km(0.3) == "less than 1 km"
    assert fmt.litres(12000) == "12,000 L" and fmt.fill(87.4) == "87%" and fmt.fill(130) == "overflowing"
    assert fmt.day_name("2026-10-18") == "Sunday 18 October"
    assert fmt.day_name(date(2026, 10, 5)) == "Monday 5 October"
    ts = int(datetime(2026, 10, 18, 6, 5, tzinfo=config.IST).timestamp())
    assert fmt.hhmm(ts) == "06:05" and fmt.when(ts) == "18 Oct 06:05"
    assert fmt.duration(150) == "2 h 30 min" and fmt.duration(40) == "40 min"


def test_one_colour_scale():
    assert fmt.status_now(100) == "red"
    assert fmt.status_now(80, fill_in_3h=101) == "orange"
    assert fmt.status_now(60, fill_in_3h=90) == "amber" and fmt.status_now(20, 40) == "green"
    assert fmt.status_plan(100, 120) == "red" and fmt.status_plan(80, 100) == "orange"
    assert fmt.status_plan(30, 55) == "amber" and fmt.status_plan(10, 20) == "green"
    assert set(fmt.STATUS_COLOURS) == {"red", "orange", "amber", "green"}


def test_schools_are_not_due_on_sundays(conn):
    bins = db.bins(conn)
    schools = [b for b in bins if b["site_type"] == "school"]
    assert schools and all(db.rest_days(b) == {6} for b in schools)
    office = [b for b in bins if b["site_type"] == "public_office"]
    assert all(b["collect_every_days"] == 2 for b in office)
    sunday, monday = date(2026, 10, 18), date(2026, 10, 19)
    due_sun = planner.due_status(conn, schools, sunday, NOW_TS)
    due_mon = planner.due_status(conn, schools, monday, NOW_TS)
    assert not any(d["due"] for d in due_sun.values())
    assert all(d["rest_day"] for d in due_sun.values())
    assert any(d["due"] for d in due_mon.values())


def test_synthetic_routine_skips_sundays_for_schools(conn):
    schools = {b["id"] for b in db.bins(conn) if b["site_type"] == "school"}
    days = {datetime.fromtimestamp(r[0], config.IST).weekday() for r in conn.execute(
        f"SELECT ts FROM events WHERE kind='collected' AND synthetic=1 AND bin_id IN ({','.join('?' * len(schools))})",
        tuple(schools))}
    assert days and 6 not in days


def test_old_database_gets_the_new_column_and_register(tmp_path, template_db):
    import shutil
    import sqlite3
    path = tmp_path / "old.db"
    shutil.copy(template_db, path)
    raw = sqlite3.connect(path)
    raw.execute("UPDATE bins SET rest_days='', collect_every_days=1 WHERE site_type IN ('school','public_office')")
    raw.execute("DELETE FROM meta WHERE key='demo_register_rev'")
    raw.commit()
    raw.close()
    db._INITIALISED.discard(str(path))
    c = db.connect(path)
    assert all(db.rest_days(b) == {6} for b in db.bins(c) if b["site_type"] == "school")
    assert all(b["collect_every_days"] == 2 for b in db.bins(c) if b["site_type"] == "public_office")
    c.close()


def test_plan_made_in_the_background_reports_progress(conn, tmp_path):
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    seen = []

    def make(c, progress):
        def spy(phase, it, km):
            seen.append((phase, it, km))
            progress(phase, it, km)
        return planner.generate(c, "tester", now_ts=NOW_TS, seconds=2.0, progress=spy)

    job = jobs.start("main:test", "plan", "tester", make, db_path=path)
    assert jobs.start("main:test", "plan", "someone else", make, db_path=path) is job   # one job at a time
    job = jobs.wait("main:test", 120)
    assert job.status == "done", job.error
    assert planner.get(conn, job.plan_id)["status"] == "draft"
    phases = [p for p, _, _ in seen]
    assert phases[0] == "data" and "search" in phases and phases[-1] == "check"
    kms = [k for p, _, k in seen if p == "search" and k is not None]
    assert kms and all(k > 0 for k in kms)
    assert job.iterations > 0 and job.best_km > 0


def test_failed_background_plan_says_why(conn):
    path = conn.execute("PRAGMA database_list").fetchone()[2]

    def make(c, progress):
        raise planner.PlanError("No truck is available")

    job = jobs.wait(jobs.start("main:fail", "plan", "tester", make, db_path=path).key)
    assert job.status == "failed" and job.error == "No truck is available"


def test_demo_history_rolls_forward_when_old(conn):
    info = json.loads(db.get_meta(conn, "synthetic_info"))
    assert live.roll_forward_if_old(conn, now_ts=info["end_ts"] + 3600) is None      # 1 h old: kept
    new = live.roll_forward_if_old(conn, now_ts=info["end_ts"] + 3 * 3600)           # 3 h old: rolled
    assert new and new["end_ts"] > info["end_ts"] and new["days"] == info["days"]
    assert live.feed_on(conn)                          # demonstration mode: feed on by default
    with db.tx(conn):
        db.set_setting(conn, "demo_feed", False)
    assert not live.feed_on(conn)


def test_plan_explains_bins_added_although_not_due(conn):
    p = planner.generate(conn, "tester", now_ts=NOW_TS, seconds=2.0)
    stops = [s for r in p["routes"] for s in r["stops"] if s["kind"] == "bin"]
    assert all("due" in s for s in stops)
    assert all(s["expected_fill_pct"] >= 40 for s in stops if not s["due"])
    _ = synth  # the synthetic history the plan is built on


def test_fast_route_cost_matches_the_reference(conn):
    import random
    ctx = planner.build_context(conn, NOW_TS, "daily", None, set())
    prob = ctx.prob
    rng = random.Random(7)
    for _ in range(200):
        k = rng.randrange(len(prob.vehicles))
        seq = rng.sample(range(prob.nb), rng.randint(0, prob.nb))
        assert prob.route_cost(k, seq) == prob.route_cost_reference(k, seq)
