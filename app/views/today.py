"""Today's plan: Plan · Progress · Live alerts · Briefing & driver messages · Crew phone view."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from wastewise import db, planner, roads

from ui import maps, sheets
from ui.common import (LEVEL_ICON, STATUS_WORDS, can, conn, hhmm, live_refresh, log, mark_seen, synthetic_notice,
                       today_label, user, when)


def _new_since(plan: dict) -> tuple[int, int]:
    c = conn()
    m = plan.get("data_marks", {})
    r = c.execute("SELECT COUNT(*) FROM readings WHERE id > ? AND source <> 'sensor'",
                  (m.get("max_reading_id", 0),)).fetchone()[0]
    s = c.execute("SELECT COUNT(*) FROM readings WHERE id > ? AND source = 'sensor'",
                  (m.get("max_reading_id", 0),)).fetchone()[0]
    k = c.execute("SELECT COUNT(*) FROM complaints WHERE id > ?", (m.get("max_complaint_id", 0),)).fetchone()[0]
    return r + s, k


def _plan_choices(day) -> list[dict]:
    c = conn()
    rows = db.rows(c, "SELECT id, status, kind, created_at, created_by FROM plans WHERE plan_date=? AND kind<>'whatif'"
                      " AND status<>'superseded' ORDER BY id DESC LIMIT 6", (day.isoformat(),))
    return rows


def _generate(kind: str, base: dict | None = None) -> None:
    u = user()
    c = conn()
    try:
        with st.spinner("Planning routes on the real street network ... (about 10 seconds)"):
            p = planner.generate(c, u["username"], kind=kind, replan_of=base)
        pid = planner.save(c, p, u["username"])
        st.session_state.plan_view = pid
        mark_seen()
        st.toast(f"Plan #{pid} ready (draft). Check it, then approve.", icon=":material/check_circle:")
    except planner.PlanError as exc:
        st.error(str(exc))


def kpi_row(p: dict) -> None:
    k, b = p["kpis"], p["baseline"]
    cols = st.columns(6)
    cols[0].metric("Bins collected", k["bins_served"], f"{k['bins_served'] - b['bins_served']:+d} vs routine",
                   delta_color="normal")
    cols[1].metric("Waste", f"{k['litres'] / 1000:.1f} m³")
    cols[2].metric("Distance", f"{k['distance_km']:.1f} km", f"{k['distance_km'] - b['distance_km']:+.1f} km",
                   delta_color="inverse")
    cols[3].metric("Fuel / CO₂", f"{k['fuel_l']:.0f} L / {k['co2_kg']:.0f} kg",
                   f"{k['co2_kg'] - b['co2_kg']:+.0f} kg CO₂", delta_color="inverse")
    cols[4].metric("Expected overflow", f"{k['expected_overflow_min']:.0f} bin-min",
                   f"{k['expected_overflow_min'] - b['expected_overflow_min']:+.0f}", delta_color="inverse")
    cols[5].metric("Landfill trips", k["landfill_trips"], f"finish {hhmm(k['finish_ts'])}", delta_color="off")
    st.caption("Deltas compare with the nearest-bin routine on the same day and data"
               + (" (demonstration data)." if p.get("label") else "."))


def show_alerts(alerts: list[dict]) -> None:
    for a in alerts:
        fn = {"critical": st.error, "warning": st.warning}.get(a["level"], st.info)
        txt = f"**{a['text']}**  \nWhat to do: {a['action']}"
        if a.get("bins"):
            txt += f"  \nBins: {', '.join(a['bins'][:12])}"
        fn(txt, icon=LEVEL_ICON.get(a["level"]))


def route_table(route: dict) -> pd.DataFrame:
    rows = []
    n = 0
    for s in route["stops"]:
        if s["kind"] == "bin":
            n += 1
            rows.append({"#": str(n), "Time": hhmm(s["arrive_ts"]), "Bin": s["bin_id"], "Place": s["landmark"],
                         "Street": s["street"], "Expected fill": f"{s['expected_fill_pct']:.0f}%",
                         "Status": STATUS_WORDS[s["status"]], "Pick-up (L)": s["pickup_l"],
                         "Load after (L)": s["load_after_l"], "Own area": "yes" if s["own_beat"] else "helping"})
        elif s["kind"] == "landfill":
            rows.append({"#": "", "Time": hhmm(s["arrive_ts"]), "Bin": "LANDFILL", "Place": "Turbhe landfill",
                         "Street": f"unload {(s['depart_ts'] - s['arrive_ts']) / 60:.0f} min", "Expected fill": "",
                         "Status": "", "Pick-up (L)": None, "Load after (L)": 0, "Own area": ""})
        else:
            rows.append({"#": "", "Time": hhmm(s["arrive_ts"]), "Bin": "DEPOT", "Place": "Back at the depot",
                         "Street": "", "Expected fill": "", "Status": "", "Pick-up (L)": None,
                         "Load after (L)": s["load_after_l"], "Own area": ""})
    return pd.DataFrame(rows)


def plan_tab(day) -> None:
    c = conn()
    settings = db.get_settings(c)
    shift_now = planner.in_shift(c)
    choices = _plan_choices(day)
    approved = planner.current(c, day)
    approved = approved if approved and approved["status"] == "approved" else None

    # Which plan to show: the approved one unless the user picked a newer draft.
    view_id = st.session_state.get("plan_view")
    ids = [r["id"] for r in choices]
    if view_id not in ids:
        view_id = approved["id"] if approved else (ids[0] if ids else None)
    if len(choices) > 1:
        labels = {r["id"]: f"#{r['id']} {r['status']} {r['kind']} · {when(r['created_at'])}" for r in choices}
        view_id = st.radio("Plan", ids, index=ids.index(view_id), format_func=labels.get, horizontal=True,
                           key="plan_pick")
        st.session_state.plan_view = view_id
    p = planner.get(c, view_id) if view_id else None

    b1, b2, b3, _ = st.columns([1.2, 1, 1.3, 3])
    if can("generate_plan"):
        if b1.button("Generate plan", type="primary" if p is None else "secondary", disabled=shift_now,
                     help="Plan the next shift from the latest data. During the shift use Re-plan from now."):
            _generate("daily")
            st.rerun()
    if p and can("approve_plan") and p["status"] == "draft":
        if b2.button("Approve plan", type="primary", disabled=not p["verifier"]["ok"]):
            planner.approve(c, p["id"], user()["username"])
            mark_seen()
            st.rerun()
    if can("replan"):
        if b3.button("Re-plan from now", disabled=not (shift_now and approved),
                     help="Only during shift hours, starting each truck from where it is now."):
            _generate("replan", approved)
            st.rerun()

    if p is None:
        st.info(f"No plan yet for **{today_label(day)}**. Nothing is computed until someone presses "
                "**Generate plan**.", icon=":material/event_note:")
        return

    badge = "ww-approved" if p["status"] == "approved" else "ww-draft"
    st.markdown(
        f"<span class='ww-badge {badge}'>{p['status'].upper()}</span> &nbsp; Plan #{p['id']} "
        f"({'re-plan' if p['kind'] == 'replan' else 'daily plan'}) for <b>{today_label(p['plan_date'])}</b> · "
        f"made {when(p['created_at'])} by {p['created_by']}"
        + (f" · approved by {p['approved_by']} at {hhmm(p['approved_at'])}" if p.get("approved_by") else "")
        + f" · forecast: {p['forecast_source']}", unsafe_allow_html=True)

    n_read, n_comp = _new_since(p)
    if n_read or n_comp:
        st.warning(f"**New information since this plan was made:** {n_read} new reading(s), {n_comp} new "
                   f"complaint(s). " + ("Use **Re-plan from now** if they change priorities."
                                        if shift_now else "Generate the plan again before the shift if needed."),
                   icon=":material/notifications_active:")
    if not p["verifier"]["ok"]:
        st.error("This plan failed the independent check and cannot be approved: "
                 + "; ".join(p["verifier"]["errors"][:3]))
    show_alerts(p["alerts"])
    kpi_row(p)

    vehicles = [r["vehicle_id"] for r in p["routes"]]
    names = {r["vehicle_id"]: r["vehicle_name"] for r in p["routes"]}
    sel = st.pills("Show trucks", vehicles, selection_mode="multi", default=vehicles, format_func=names.get,
                   key="truck_pills") or vehicles
    net = roads.network_for(c) if settings.get("map_tiles") == "offline" else None
    fig = maps.plan_map(p, db.places(c), db.bins(c), net, sel, tiles=settings.get("map_tiles", "osm"))
    st.plotly_chart(fig, use_container_width=True, config={"displaylogo": False, "scrollZoom": True})
    st.caption("Ring and line = truck colour. Centre: red = full at shift start, orange = full by the time the "
               "truck arrives, amber = over half full, green = fine. Grey = not in this plan; black = due but "
               "left out. Numbers are the stop order.")

    for r in p["routes"]:
        if r["vehicle_id"] not in sel:
            continue
        t = r["totals"]
        with st.expander(f"{r['vehicle_name']} · {t['bins']} bins · {t['landfill_trips']} landfill trip(s) · "
                         f"{t['distance_km']} km · {hhmm(r['start_ts'])}-{hhmm(t['end_ts'])}"
                         + (f" · helps another area at {t['off_beat_bins']} bin(s)" if t["off_beat_bins"] else ""),
                         expanded=False):
            st.dataframe(route_table(r), hide_index=True, use_container_width=True)
            crew = db.one(c, "SELECT display_name FROM users WHERE vehicle_id=? AND role='crew'", (r["vehicle_id"],))
            html = sheets.route_sheet_html(p, r, crew["display_name"] if crew else None)
            if st.download_button("Printable route sheet", html, file_name=f"route_{p['plan_date']}_{r['vehicle_id']}.html",
                                  mime="text/html", key=f"sheet_{p['id']}_{r['vehicle_id']}",
                                  icon=":material/print:"):
                log("route_sheet_downloaded", {"plan_id": p["id"], "vehicle": r["vehicle_id"]})
    if p["unserved"]:
        with st.expander(f"Bins not in this plan ({len(p['unserved'])})"):
            st.dataframe(pd.DataFrame([{"Bin": u["bin_id"], "Place": u["landmark"], "Due": "yes" if u["due"] else "no",
                                        "Why": u["reason"], "Expected (L)": u["expected_l"]} for u in p["unserved"]]),
                         hide_index=True, use_container_width=True)
    with st.expander("How this plan was checked"):
        v = p["verifier"]
        st.write(f"Independent verifier: **{'passed' if v['ok'] else 'FAILED'}** · {v['bins_checked']} bins and "
                 f"{v['routes_checked']} routes re-checked (times, capacity, final unload, shift end, duplicates, "
                 f"due bins, totals). Recomputed distance {v['distance_km']} km.")
        for e in v["errors"]:
            st.write(":material/error:", e)
        for w in v["warnings"]:
            st.write(":material/warning:", w)
        s = p["solver"]
        st.write(f"Optimiser (Adaptive Large Neighbourhood Search): {s['iterations']} iterations in "
                 f"{s['seconds']} s, cost {s['initial_cost']:.0f} → {s['best_cost']:.0f}. "
                 f"{p['candidates']} candidate bins ({p['due_bins']} due).")
        if p.get("bad_sensors"):
            st.write("Sensors not trusted today (faulty/offline):", ", ".join(p["bad_sensors"]))


def page() -> None:
    live_refresh()
    c = conn()
    day = planner.plan_day(c)
    st.title("Today's plan")
    synthetic_notice()
    tabs = st.tabs(["Plan", "Progress", "Live alerts", "Briefing & driver messages", "Crew phone view"])
    with tabs[0]:
        plan_tab(day)
    from views import progress, alerts_view, briefing, crew
    with tabs[1]:
        progress.tab(day)
    with tabs[2]:
        alerts_view.tab(day)
    with tabs[3]:
        briefing.tab(day)
    with tabs[4]:
        crew.preview(day)
