"""The live forecast path (data cut at the forecast time) must build the same inputs as training does, and
Live alerts are predictions from it."""
import numpy as np

from conftest import NOW_TS
from wastewise import db, forecast, live, outlook_now


def test_future_windows_use_earlier_days_not_the_last_hour(conn):
    """'Same hour on earlier days' for a window that starts after the end of the data must equal the value
    the same window gets when the data continues (it only looks at earlier days)."""
    bins = [b for b in db.bins(conn) if b["has_sensor"]][:5]
    cut = NOW_TS - 2 * 86400
    F = forecast.FEATURES
    cols = [F.index("same_hour_7d"), F.index("same_how_4w"), F.index("hour")]
    checked = 0
    for s_cut, s_full in zip(forecast.sanpada_series(conn, cut - 29 * 86400, cut, bins),
                             forecast.sanpada_series(conn, cut - 29 * 86400, NOW_TS, bins)):
        o = np.array([len(s_cut.f) - 1])
        Xc, _, _, kc = forecast.feature_rows(s_cut, o, [0, 6, 12, 18], with_target=False)
        Xf, _, _, kf = forecast.feature_rows(s_full, o, [0, 6, 12, 18], with_target=False)
        assert np.allclose(Xc[:, cols], Xf[:, cols], equal_nan=True)
        # and the forecast is not flat across the day
        assert len(set(np.round(Xc[:, cols[0]], 3))) > 1
        checked += 1
    assert checked


def test_live_alerts_are_predictions(conn):
    al = live.alerts(conn, NOW_TS)
    pred = outlook_now.predict(conn, NOW_TS)
    assert set(pred["bins"]) == {b["id"] for b in db.bins(conn)}
    p = [a for a in al if a["kind"] == "predicted"]
    for a in p:
        pr = pred["bins"][a["bin_id"]]
        assert pr["overflow_min"] >= 30 or "complaint" in a["text"]
        assert ("ML forecaster" in a["text"]) or ("learned pattern" in a["text"])
    assert not any(a["kind"] in ("complaint_ready", "evening") for a in al)     # shown elsewhere, not repeated


def test_advice_depends_on_the_time_of_day(conn):
    s = db.get_settings(conn)
    from wastewise import planner
    start, end = planner.shift_window(s, planner.today(NOW_TS))
    assert "Re-plan" in outlook_now.advice(conn, start + 3600, None)
    assert "evening round" in outlook_now.advice(conn, end + 3600, None)
    assert "Re-plan" not in outlook_now.advice(conn, start - 3600, None)


def test_crew_work_is_visible_for_the_day_whatever_route_it_belongs_to(conn):
    from wastewise import planner, progress
    p = planner.generate(conn, "op", now_ts=NOW_TS, seconds=2.0)
    pid = planner.save(conn, p, "op")
    planner.approve(conn, pid, "supervisor")
    plan = planner.get(conn, pid)
    r = next(x for x in plan["routes"] if x["stops"])
    stop = next(s for s in r["stops"] if s["kind"] == "bin")
    tap = NOW_TS - 3600                          # recorded the evening before, on the next shift's route
    progress.record_crew(conn, plan, r["vehicle_id"], "collected", "crew1", stop["bin_id"], 75.0, ts=tap,
                         person="Ramesh Patil (demo)")
    progress.record_crew(conn, plan, r["vehicle_id"], "landfill_unload", "crew1", ts=tap + 600, person="Ramesh")
    from datetime import datetime
    from wastewise import config
    day = datetime.fromtimestamp(tap, config.IST).date().isoformat()
    rows = [x for x in progress.day_activity(conn, day) if not x["synthetic"]]
    assert [x["what"] for x in rows] == ["Collected", "Unloaded at the landfill"]
    assert rows[0]["by"] == "Ramesh Patil (demo)" and rows[0]["place"] == stop["landmark"]
    assert (plan["plan_date"] == day) or ("plan for" in rows[0]["route"])
    s = progress.activity_summary(rows)[r["vehicle_name"]]
    assert s["collected"] == 1 and s["unloads"] == 1
