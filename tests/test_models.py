import json
import warnings
from datetime import datetime, timezone

import numpy as np
import pytest

from wastewise import anomaly, db, forecast, telum
from conftest import NOW_TS

warnings.filterwarnings("ignore")


@pytest.fixture(scope="module")
def trained(template_db, tmp_path_factory):
    import shutil
    from wastewise import config
    path = tmp_path_factory.mktemp("models") / "m.db"
    shutil.copy(template_db, path)
    old = config.MODELS_DIR
    config.MODELS_DIR = tmp_path_factory.mktemp("model_files")
    conn = db.connect(path)
    m1 = forecast.train_and_register(conn, "test", end_ts=NOW_TS, history_days=60)
    m2 = forecast.train_and_register(conn, "test", end_ts=NOW_TS, history_days=60)
    yield conn, m1, m2
    conn.close()
    config.MODELS_DIR = old


def test_forecaster_beats_naive_baselines(trained):
    _, m1, _ = trained
    t = m1["test"]
    best = t[m1["best_model"]]["mae_3h_points"]
    assert best < t["last_24_hours"]["mae_3h_points"]
    assert best < t["mean_rate_last_7_days"]["mae_3h_points"]
    assert "SYNTHETIC" in m1["label"]


def test_locked_test_window_and_promotion_rule(trained):
    conn, m1, m2 = trained
    assert m1["test_window"] == m2["test_window"]          # same locked window
    assert m1["decision"]["promoted"]
    assert not m2["decision"]["promoted"]                  # not better -> not promoted
    n = db.one(conn, "SELECT COUNT(*) n FROM models WHERE kind='forecaster' AND promoted=1")["n"]
    assert n == 1


def test_chronological_split_has_gaps():
    ots = np.arange(0, 100 * 86400, 3600)
    tr, va, te, b = forecast.chrono_split(ots)
    assert ots[tr].max() + 24 * 3600 < ots[va].min()
    assert ots[va].max() + 24 * 3600 < ots[te].min()
    assert not (tr & va).any() and not (va & te).any()


def test_features_use_only_the_past():
    """Changing the future must not change features at the origin."""
    H = 24 * 30
    f = np.linspace(0, 80, H) % 90
    s = forecast.Series("x", 1_790_000_000, f.copy(), np.zeros(H, bool), {}, forecast.config.IST)
    o = np.array([H - 100])
    X1, _, _, _ = forecast.feature_rows(s, o, [0, 3, 6, 21], with_target=False)
    s.f[H - 99:] = 5.0
    X2, _, _, _ = forecast.feature_rows(s, o, [0, 3, 6, 21], with_target=False)
    assert np.allclose(np.nan_to_num(X1, nan=-9), np.nan_to_num(X2, nan=-9))


def test_predict_rates_for_planner(trained):
    conn, _, _ = trained
    bins = db.bins(conn)
    from wastewise import state
    prof = state.rate_profiles(conn, NOW_TS)
    st = state.estimate(conn, NOW_TS, prof)
    fb = {b["id"]: state.profile_rates(prof[b["id"]], NOW_TS, 25) for b in bins}
    out = forecast.predict_rates(conn, NOW_TS, 25, bins, st, fallback=fb)
    assert out is not None and "ML forecaster" in out["source"]
    assert set(out["rates"]) == {b["id"] for b in bins}
    for r in out["rates"].values():
        assert len(r) == 25 and (r >= 0).all()


# ---------------------------------------------------------------- anomaly rules
def test_rules_flag_stuck_spike_and_ignore_collections():
    ts = np.arange(48) * 3600
    v = np.minimum(np.arange(48) * 2.0, 90)
    v[30:] = v[30:] - 60          # a collection at hour 30 (drop) ...
    coll = np.array([30 * 3600 - 600])
    v[10] = 100.0                 # ... a spike at hour 10
    v[35:48] = v[35]              # ... and a stuck sensor from hour 35
    fl = anomaly.rule_flags(ts, v, coll)
    assert fl["spike"][10] and not fl["spike"][30]
    assert fl["stuck"][40] and not fl["stuck"][20]


def test_no_drop_after_collection():
    ts = np.arange(10) * 3600
    v = np.full(10, 70.0) + np.arange(10)
    fl = anomaly.rule_flags(ts, v, np.array([3 * 3600 + 60]))
    assert fl["no_drop"][5]


def test_faulty_sensor_detected_and_excluded(conn):
    b = next(x for x in db.bins(conn) if x["has_sensor"])["id"]
    for h in range(20):
        conn.execute("INSERT INTO readings(bin_id, ts, fill_pct, source, synthetic) VALUES (?,?,?,?,0)",
                     (b, NOW_TS + 3600 * h, 42.0, "api"))
    health = anomaly.sensor_health(conn, NOW_TS + 3600 * 19)
    assert health[b]["status"] == "faulty" and "stuck" in health[b]["reasons"]


def test_surge_detected(conn):
    b = next(x for x in db.bins(conn) if not x["has_sensor"])["id"]
    for h, f in enumerate([20, 35, 50, 65, 80]):
        db.add_reading(conn, b, f, "supervisor", ts=NOW_TS + 3600 * h, user="t")
    s = anomaly.surges(conn, NOW_TS + 3600 * 4 + 60)
    assert any(x["bin_id"] == b for x in s)


def test_fault_evaluation_reports_by_type(conn):
    r = anomaly.evaluate_synthetic(conn, days=30)
    assert set(r["recall_by_type"]) == {"stuck", "drift", "spike"}
    assert r["recall_by_type"]["stuck"] > 0.8


# ---------------------------------------------------------------- Mildura parsing
def test_mildura_parser(tmp_path):
    rows = ["Timestamp (UTC),Device Tag,Channel Description,Raw Value,Calibration,Offset,Calibrated Value,"
            "Sensor Type,Unit,Location Lat,Location Long"]
    t0 = 1_650_000_000
    for h in range(800):
        d = 1200 - (h % 100) * 9          # fills for 100 h, then emptied
        if h == 50:
            d = 9999                      # error code
        ts = datetime.fromtimestamp(t0 + h * 3600, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows.append(f"{ts},mil-binX,Distance to Top,{d},1,0,{d},,mm,-34.1,142.1")
    p = tmp_path / "m.csv"
    p.write_text("﻿" + "\n".join(rows), encoding="utf-8")
    s = forecast.mildura_series(p)
    assert len(s) == 1
    assert s[0].coll.sum() >= 6
    assert np.nanmax(s[0].f) <= 110 and np.nanmin(s[0].f) >= 0


# ---------------------------------------------------------------- Telum / Snap ML
def test_telum_engine_matches_native(trained, tmp_path):
    if telum.snapml_version() is None:
        pytest.skip("IBM Snap ML not installed (LinuxONE only)")
    conn, _, _ = trained
    cur = forecast.promoted(conn)
    m = forecast._load(cur["path"])
    X = np.random.default_rng(1).uniform(0, 40, (500, len(forecast.FEATURES)))
    ref = m.gbt_predict(forecast._fill_nan(X))
    for mode in ("auto", "accelerator"):
        if mode == "accelerator" and not telum.accelerator_available():
            continue
        e = telum.engine_for(m, __import__("pathlib").Path(cur["path"]), mode)
        assert e is not None
        p = e.predict(forecast._fill_nan(X))
        assert np.abs(p - ref).mean() < 0.01
