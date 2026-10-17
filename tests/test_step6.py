"""Step 6: Granite self-check gates the language-model features; forecasts go through one engine path."""
import json

import numpy as np

from wastewise import db, forecast, llm, telum


def test_selfcheck_without_a_model_offers_nothing(empty_conn, monkeypatch):
    monkeypatch.setattr(llm, "status", lambda c: {"ok": False, "model": None, "error": "connection refused"})
    r = llm.selfcheck(empty_conn)
    assert not r["ran"] and "not running" in r["reason"]
    assert not llm.any_usable(empty_conn) and not llm.selfcheck_needed(empty_conn)


def test_only_tasks_that_pass_the_figure_check_are_offered(empty_conn, monkeypatch):
    monkeypatch.setattr(llm, "status", lambda c: {"ok": True, "model": "granite-test", "error": None})
    f = llm.SAMPLE_FACTS

    def fake_chat(conn, system, user, max_tokens=350, temperature=0.2):
        if "pre-shift briefing" in user:      # correct figures only
            return (f"Briefing: {f['trucks']} trucks collect {f['bins_total']} bins, {f['distance_km']} km, "
                    f"back by {f['last_truck_back']}.")
        return "Today 99 bins, start 05:55."         # invented figures -> rejected
    monkeypatch.setattr(llm, "chat", fake_chat)
    r = llm.selfcheck(empty_conn)
    assert r["kinds"]["briefing"]["ok"] and not r["kinds"]["driver_en"]["ok"]
    assert llm.usable(empty_conn, "briefing") and not llm.usable(empty_conn, "driver_hi")
    assert llm.any_usable(empty_conn)
    # a different model on the server must be checked again before it is offered
    monkeypatch.setattr(llm, "status", lambda c: {"ok": True, "model": "other-model", "error": None})
    assert not llm.usable(empty_conn, "briefing") and llm.selfcheck_needed(empty_conn)


def test_engine_summary_states_what_runs_where():
    class M:
        def predict(self, X):
            return np.zeros(len(X))
    cpu = telum.Engine("CPU (Snap ML compressed trees)", "compress_trees", M(),
                       timings_ms={"cpu": {"720": 0.8, "20000": 22.0}, "accelerator": {"720": 1.5, "20000": 40.0}})
    assert cpu.summary().startswith("Telum tested - CPU faster")
    acc = telum.Engine("IBM Telum AI accelerator (Snap ML, zDNN)", "zdnn_tensors", M())
    mixed = telum.Engine("CPU (Snap ML compressed trees)", "compress_trees", M(), bulk=acc,
                         timings_ms={"cpu": {"720": 0.8, "20000": 40.0}, "accelerator": {"720": 1.5, "20000": 22.0}})
    assert mixed.device_for(720).startswith("CPU") and mixed.device_for(50000).startswith("IBM Telum")
    assert "bulk forecasts on Telum" in mixed.summary()


def test_bulk_jobs_are_logged_with_their_engine(conn):
    assert forecast.predict_with_engine(conn, np.zeros((3, len(forecast.FEATURES))), "test") is None   # no model yet
