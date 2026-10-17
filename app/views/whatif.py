"""What-if planning: truck breakdown, festival / market rush in an area, extra truck. Compare and adopt."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from wastewise import audit, db, planner

from ui import maps
from ui.common import can, conn, hhmm, mark_seen, synthetic_notice, user

ROWS = [("bins_served", "Bins collected", "{:.0f}"), ("due_left_out", "Due bins left out", "{:.0f}"),
        ("distance_km", "Distance (km)", "{:.1f}"), ("fuel_l", "Fuel (L)", "{:.1f}"),
        ("co2_kg", "CO₂ (kg)", "{:.0f}"), ("expected_overflow_min", "Expected overflow (bin-min)", "{:.0f}"),
        ("bins_full_on_arrival", "Bins full when the truck arrives", "{:.0f}"),
        ("landfill_trips", "Landfill trips", "{:.0f}")]


def _run(scenario: dict | None, seconds: float) -> dict:
    c = conn()
    shift = planner.in_shift(c)
    base = None
    if shift:
        base = planner.current(c, planner.plan_day(c))
        if not base or base["status"] != "approved":
            raise planner.PlanError("During the shift a what-if starts from the approved plan - approve one first.")
    p = planner.generate(c, user()["username"], kind="replan" if shift else "daily", scenario=scenario,
                         seconds=seconds, replan_of=base)
    return p


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
    st.write("Try a situation before it happens. WasteWise plans the day with and without it and shows the "
             "difference. Nothing changes until you adopt a plan.")
    with st.form("scenario"):
        col1, col2, col3 = st.columns(3)
        with col1.container(border=True):
            st.markdown("**Truck breakdown**")
            broken = st.multiselect("Trucks out of service", [v["id"] for v in vehicles],
                                    format_func=lambda x: next(v["name"] for v in vehicles if v["id"] == x))
        with col2.container(border=True):
            st.markdown("**Festival / market rush in an area**")
            centre = st.selectbox("Around bin", list(by_id), index=None, placeholder="no rush",
                                  format_func=lambda x: f"{x} {by_id[x]['landmark']}")
            radius = st.slider("Radius (m)", 150, 1000, 400, 50)
            factor = st.slider("Waste compared with normal", 1.2, 3.0, 1.8, 0.1, format="%.1fx")
        with col3.container(border=True):
            st.markdown("**Extra truck**")
            extra = st.checkbox("Add a spare truck for the day")
            extra_cap = st.select_slider("Its capacity (L)", [6000, 9000, 12000], value=6000)
        seconds = st.slider("Planning time per plan (seconds)", 3, 15, 6)
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
            with st.spinner("Planning the normal day and the what-if ..."):
                normal = _run(None, seconds)
                what = _run(scenario, seconds)
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
        parts.append("breakdown of " + ", ".join(scenario["breakdown"]))
    if scenario.get("surge"):
        s = scenario["surge"]
        parts.append(f"{s['factor']:.1f}x waste within {s['radius_m']} m of {s['center_bin']}")
    if scenario.get("extra_truck"):
        parts.append(f"spare truck {scenario['extra_truck']['capacity_l']:,} L")
    st.subheader("What-if: " + "; ".join(parts))
    rows = []
    for key, label, fmt in ROWS:
        a, b = normal["kpis"][key], what["kpis"][key]
        rows.append({"": label, "Normal plan": fmt.format(a), "What-if plan": fmt.format(b),
                     "Change": ("+" if b - a > 0 else "") + fmt.format(b - a)})
    rows.append({"": "Last truck back", "Normal plan": hhmm(normal["kpis"]["finish_ts"]),
                 "What-if plan": hhmm(what["kpis"]["finish_ts"]), "Change": ""})
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
    for a in what["alerts"]:
        if a["level"] in ("critical", "warning"):
            (st.error if a["level"] == "critical" else st.warning)(f"**{a['text']}**  \nWhat to do: {a['action']}")
    if not what["verifier"]["ok"]:
        st.error("The what-if plan failed the independent check: " + "; ".join(what["verifier"]["errors"][:3]))
    st.plotly_chart(maps.plan_map(what, db.places(c), bins, tiles=db.get_settings(c).get("map_tiles", "osm"),
                                  height=520), use_container_width=True, config={"displaylogo": False})
    if what.get("label"):
        st.caption("Computed from demonstration (synthetic) data.")
    if st.button("Adopt the what-if plan as today's draft", type="primary", disabled=not what["verifier"]["ok"]):
        what = dict(what, scenario=scenario)
        pid = planner.save(c, what, user()["username"])
        audit.log(c, user()["username"], "whatif_adopted", {"plan_id": pid, "scenario": scenario})
        st.session_state.plan_view = pid
        st.session_state.pop("whatif")
        mark_seen()
        st.success(f"Saved as draft plan #{pid}. A supervisor approves it under Today's plan.")
