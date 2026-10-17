"""What-if planning: truck breakdown, festival / market rush in an area, extra truck. Compare and adopt."""
from __future__ import annotations

import streamlit as st

from wastewise import audit, db, planner

from ui import maps
from ui.common import can, conn, duration, hhmm, mark_seen, synthetic_notice, user, vehicle_names
from ui.tables import Col, table

# key, label, how to show a value, +1 if more is better / -1 if less is better / 0 neutral
ROWS = [("bins_served", "Bins collected", lambda v: f"{v:.0f} bins", +1),
        ("due_left_out", "Due bins that do not fit", lambda v: f"{v:.0f} bins", -1),
        ("distance_km", "Driving", lambda v: f"{v:.0f} km", -1),
        ("fuel_l", "Diesel", lambda v: f"{v:.0f} L", -1),
        ("co2_kg", "CO₂", lambda v: f"{v:.0f} kg", -1),
        ("expected_overflow_min", "Time bins spend overflowing", duration, -1),
        ("bins_full_on_arrival", "Bins already full when the truck arrives", lambda v: f"{v:.0f} bins", -1),
        ("landfill_trips", "Landfill trips", lambda v: f"{v:.0f}", 0)]


def _run(scenario: dict | None) -> dict:
    c = conn()
    shift = planner.in_shift(c)
    base = None
    if shift:
        base = planner.current(c, planner.plan_day(c))
        if not base or base["status"] != "approved":
            raise planner.PlanError("During the shift a what-if starts from the approved plan - approve one first.")
    # Same search for both plans: it runs until it stops finding better routes.
    return planner.generate(c, user()["username"], kind="replan" if shift else "daily", scenario=scenario,
                            replan_of=base)


def comparison_rows(normal: dict, what: dict) -> list[dict]:
    rows = []
    for key, label, show, better in ROWS:
        a, b = normal["kpis"][key], what["kpis"][key]
        if show(a) == show(b) or show(abs(b - a)) in ("0 km", "0 L", "0 kg", "0 bins", "0", "0 min"):
            ch = ("no change", "grey")
        else:
            good = (b - a) * better > 0
            word = "more" if b > a else ("fewer" if "bins" in show(1) else "less")
            ch = (f"{show(abs(b - a))} {word}", "grey" if better == 0 else ("green" if good else "red"))
        rows.append({"what": label, "normal": show(a), "whatif": show(b), "change": ch})
    rows.append({"what": "Last truck back at the depot", "normal": hhmm(normal["kpis"]["finish_ts"]),
                 "whatif": hhmm(what["kpis"]["finish_ts"]), "change": None})
    return rows


def page() -> None:
    st.title("What-if planning")
    synthetic_notice()
    if not can("whatif"):
        st.info("Your role can view but not run what-if plans.")
        return
    c = conn()
    bins = db.bins(c)
    by_id = {b["id"]: b for b in bins}
    vehicles = db.vehicles(c)
    names = vehicle_names()
    st.write("Try a situation before it happens. WasteWise plans the day with and without it and shows the "
             "difference. Nothing changes until you adopt a plan.")
    with st.form("scenario"):
        col1, col2, col3 = st.columns(3)
        with col1.container(border=True):
            st.markdown("**A truck breaks down**")
            broken = st.multiselect("Trucks out of service", [v["id"] for v in vehicles], format_func=names.get)
        with col2.container(border=True):
            st.markdown("**Festival or market rush**")
            centre = st.selectbox("Around", list(by_id), index=None, placeholder="no rush",
                                  format_func=lambda x: f"{by_id[x]['landmark']} - {by_id[x]['street'] or ''}")
            radius = st.slider("Area around it (metres)", 150, 1000, 400, 50)
            factor = st.slider("How much more waste than usual", 1.2, 3.0, 1.8, 0.1, format="%.1f times")
        with col3.container(border=True):
            st.markdown("**An extra truck**")
            extra = st.checkbox("Add a spare truck for the day")
            extra_cap = st.select_slider("Its size (litres)", [6000, 9000, 12000], value=6000)
        go = st.form_submit_button("Compare", type="primary")
    if go:
        scenario: dict = {}
        if broken:
            scenario["breakdown"] = broken
        if centre:
            scenario["surge"] = {"center_bin": centre, "radius_m": radius, "factor": factor,
                                 "label": f"rush around {by_id[centre]['landmark']}"}
        if extra:
            scenario["extra_truck"] = {"id": "TRK-X", "name": "Spare truck", "capacity_l": extra_cap,
                                       "color": "#1E3A8A"}
        if not scenario:
            st.warning("Choose at least one change.")
            return
        try:
            with st.spinner("Planning a normal day and the situation ..."):
                normal = _run(None)
                what = _run(scenario)
        except planner.PlanError as exc:
            st.error(str(exc))
            return
        st.session_state.whatif = (scenario, normal, what)
        audit.log(c, user()["username"], "whatif_run", {"scenario": scenario})

    if "whatif" not in st.session_state:
        return
    scenario, normal, what = st.session_state.whatif
    parts = []
    if scenario.get("breakdown"):
        parts.append(", ".join(names.get(v, v) for v in scenario["breakdown"]) + " broken down")
    if scenario.get("surge"):
        s = scenario["surge"]
        lm = by_id.get(s["center_bin"], {}).get("landmark", "the chosen place")
        parts.append(f"{s['factor']:.1f} times the usual waste within {s['radius_m']} m of {lm}")
    if scenario.get("extra_truck"):
        parts.append(f"a spare {scenario['extra_truck']['capacity_l']:,} L truck")
    st.subheader("What if: " + "; ".join(parts) + "?")
    table(comparison_rows(normal, what), [Col("what", ""), Col("normal", "Normal day"),
                                          Col("whatif", "With this situation"), Col("change", "Difference", "pill")])
    st.caption("Green = better, red = worse than a normal day.")
    for a in what["alerts"]:
        if a["level"] in ("critical", "warning"):
            (st.error if a["level"] == "critical" else st.warning)(f"**{a['text']}**  \nWhat to do: {a['action']}")
    if not what["verifier"]["ok"]:
        st.error("The what-if plan failed the independent check: " + "; ".join(what["verifier"]["errors"][:3]))
    st.plotly_chart(maps.plan_map(what, db.places(c), bins, tiles=db.get_settings(c).get("map_tiles", "osm"),
                                  height=520), use_container_width=True, config={"displaylogo": False})
    if what.get("label"):
        st.caption("Computed from demonstration (synthetic) data.")
    if st.button("Adopt this plan as today's draft", type="primary", disabled=not what["verifier"]["ok"]):
        what = dict(what, scenario=scenario)
        pid = planner.save(c, what, user()["username"])
        audit.log(c, user()["username"], "whatif_adopted", {"plan_id": pid, "scenario": scenario})
        st.session_state.plan_view = pid
        st.session_state.pop("whatif")
        mark_seen()
        st.success("Saved as today's draft plan. A supervisor approves it under Today's plan.")
