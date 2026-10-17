import json

import pytest

from wastewise import db, llm, planner, reports
from conftest import NOW_TS


@pytest.fixture()
def plan(conn):
    p = planner.generate(conn, "operator", now_ts=NOW_TS - 3600, seconds=2, seed=5)
    p["id"] = planner.save(conn, p, "operator")
    return p


# ---------------------------------------------------------------- LLM safety
def test_only_local_llm_servers():
    assert llm.is_local("http://127.0.0.1:8080") and llm.is_local("http://localhost:8080")
    assert not llm.is_local("https://api.example.com") and not llm.is_local("http://10.0.0.5:8080")


def test_templates_pass_their_own_check(plan):
    f = llm.plan_facts(plan)
    assert llm.check(llm.template_briefing(f), f)["ok"]
    for r in f["routes"]:
        for lang in ("en", "hi", "mr"):
            assert llm.check(llm.template_driver(r, lang), r)["ok"], lang


def test_check_rejects_invented_figures(plan):
    f = llm.plan_facts(plan)
    r = f["routes"][0]
    good = f"{r['truck']} starts at {r['start']} with {r['bins']} bins."
    assert llm.check(good, r)["ok"]
    bad = f"{r['truck']} starts at 05:13 with {r['bins'] + 7} bins, first SNP-099."
    c = llm.check(bad, r)
    assert not c["ok"] and "05:13" in c["unknown_numbers"] and "SNP-099" in c["unknown_bins"]
    # Devanagari digits are checked too
    assert not llm.check("आज ९९ डिब्बे", r)["ok"]


def test_falls_back_to_template_without_model(conn, plan):
    db.set_setting(conn, "llm_url", "http://127.0.0.1:9")      # nothing listens here
    res = llm.briefing(conn, plan, use_llm=True)
    assert res["source"] == "template" and "unavailable" in res["error"]
    assert str(plan["kpis"]["bins_served"]) in res["text"]


def test_rejected_llm_text_is_replaced(conn, plan, monkeypatch):
    monkeypatch.setattr(llm, "chat", lambda *a, **k: "All 999 bins are done by 04:00.")
    res = llm.briefing(conn, plan)
    assert res["source"] == "template" and res["rejected"] and "999" in res["check"]["unknown_numbers"]
    f = llm.plan_facts(plan)
    monkeypatch.setattr(llm, "chat", lambda *a, **k: f"Last truck back by {f['last_truck_back']}.")
    assert llm.briefing(conn, plan)["source"] == "Granite"


# ---------------------------------------------------------------- reports
def test_reports(conn, plan):
    planner.approve(conn, plan["id"], "supervisor")
    sizing = reports.bin_sizing(conn, NOW_TS)
    assert len(sizing) == 50 and any(s["recommendation"].startswith("too small") for s in sizing)
    hist = reports.daily_volume(conn, NOW_TS)
    assert hist and all(h["litres"] > 0 for h in hist)
    out = reports.outlook(conn, NOW_TS)
    assert len(out) == 7 and all(o["expected_litres"] > 0 and o["fleet_capacity_litres"] > 0 for o in out)
    s = reports.savings(conn)
    assert s["plans"] == 1 and s["demonstration"]
    assert s["totals"]["distance_km"]["saved"] >= 0
    pat = reports.patterns(conn, NOW_TS)
    assert pat["school"].shape == (7, 24) and pat["school"][6].sum() < pat["school"][2].sum()  # quiet Sunday


# ---------------------------------------------------------------- what-if
def test_whatif_extra_truck_and_surge(conn):
    base = planner.generate(conn, "x", now_ts=NOW_TS - 3600, seconds=2, seed=2)
    extra = planner.generate(conn, "x", now_ts=NOW_TS - 3600, seconds=2, seed=2,
                             scenario={"extra_truck": {"id": "TRK-X", "name": "Spare", "capacity_l": 6000}})
    assert extra["verifier"]["ok"] and len(extra["routes"]) == len(base["routes"]) + 1
    rush = planner.generate(conn, "x", now_ts=NOW_TS - 3600, seconds=2, seed=2,
                            scenario={"surge": {"center_bin": "SNP-040", "radius_m": 400, "factor": 2.5}})
    assert rush["verifier"]["ok"] and rush["kpis"]["litres"] > base["kpis"]["litres"]
