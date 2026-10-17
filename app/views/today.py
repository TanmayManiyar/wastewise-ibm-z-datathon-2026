"""Today's plan: Plan · Progress · Live alerts · Briefing & driver messages · Crew phone view."""
from __future__ import annotations

import streamlit as st

from datetime import datetime

from wastewise import config, db, evening, pdfs, planner, roads

from ui import maps
from ui.tables import Col, table
from ui.common import (can, change, conn, duration, hhmm, litres, live_refresh, log, mark_seen, synthetic_notice,
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
    rows = db.rows(c, "SELECT id, status, kind, created_at, created_by FROM plans WHERE plan_date=?"
                      " AND kind IN ('daily','replan') AND status<>'superseded' ORDER BY id DESC LIMIT 6",
                   (day.isoformat(),))
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
        st.toast("New plan ready (draft). Check it, then approve.", icon=":material/check_circle:")
    except planner.PlanError as exc:
        st.error(str(exc))


def route_pdf(p: dict, routes: list[dict]) -> bytes:
    """Printable PDF route sheet(s) for the given routes."""
    c = conn()
    crew = {r["vehicle_id"]: r["display_name"] for r in
            db.rows(c, "SELECT vehicle_id, display_name FROM users WHERE role='crew' AND active=1")}
    return pdfs.route_sheets(p, routes, crew, db.places(c)["depot"]["name"])


def summary_card(p: dict) -> None:
    """One sentence that says what today looks like."""
    k = p["kpis"]
    trucks = [r for r in p["routes"] if r["totals"]["bins"]]
    attention = sum(1 for a in p["alerts"] if a["level"] in ("critical", "warning"))
    left = k["due_left_out"]
    msg = (f"**{len(trucks)} trucks will collect {k['bins_served']} bins** and be back by "
           f"**{hhmm(k['finish_ts'])}**. ")
    msg += (f"**{left} due bin(s) do not fit - an extra trip is needed.** " if left else "All due bins fit. ")
    msg += (f"{attention} thing(s) need your attention below." if attention else "Nothing needs your attention.")
    with st.container(border=True):
        st.markdown(msg)


def kpi_row(p: dict) -> None:
    k, b = p["kpis"], p["baseline"]
    over_h, base_h = k["expected_overflow_min"] / 60, b["expected_overflow_min"] / 60
    cols = st.columns(4)
    cols[0].metric("Waste to collect", litres(k["litres"]), f"{k['landfill_trips']} landfill trips", delta_color="off")
    cols[1].metric("Driving", f"{k['distance_km']:.0f} km", change(k["distance_km"], b["distance_km"], "km"),
                   delta_color="inverse")
    cols[2].metric("Diesel / CO₂", f"{k['fuel_l']:.0f} L · {k['co2_kg']:.0f} kg CO₂",
                   change(k["co2_kg"], b["co2_kg"], "kg CO₂"), delta_color="inverse")
    cols[3].metric("Time bins spend overflowing", f"about {duration(k['expected_overflow_min'])}",
                   (f"{duration(abs(k['expected_overflow_min'] - b['expected_overflow_min']))} "
                    f"{'less' if over_h <= base_h else 'more'} than the old routine"), delta_color="inverse",
                   help="Expected total time, over all bins, that a bin is full before its truck arrives.")
    st.caption("'Old routine' = each truck simply drives to the nearest waiting bin, on the same day and data"
               + (" (demonstration data)." if p.get("label") else "."))


def show_alerts(alerts: list[dict]) -> None:
    """All alerts in one 'Needs attention' box: the three most important first, the rest folded."""
    important = [a for a in alerts if a["level"] in ("critical", "warning")]
    info = [a for a in alerts if a["level"] not in ("critical", "warning")]
    names = {b["id"]: b["landmark"] for b in db.bins(conn(), active_only=False)}

    def line(a):
        icon = "🔴" if a["level"] == "critical" else "🟠" if a["level"] == "warning" else "ℹ️"
        places = ""
        if a.get("bins"):
            ps = [names.get(b, b) for b in a["bins"][:4]]
            places = " · " + ", ".join(ps) + (f" and {len(a['bins']) - 4} more" if len(a["bins"]) > 4 else "")
        return f"{icon} **{a['text']}**{places}  \n&nbsp;&nbsp;&nbsp;&nbsp;→ {a['action']}"

    if not important:
        st.success("Nothing needs your attention.", icon=":material/check_circle:")
    else:
        with st.container(border=True):
            st.markdown("##### Needs attention")
            for a in important[:3]:
                st.markdown(line(a))
            if len(important) > 3:
                with st.expander(f"{len(important) - 3} more"):
                    for a in important[3:]:
                        st.markdown(line(a))
    if info:
        with st.expander("For information"):
            for a in info:
                st.markdown(line(a))


ROUTE_COLS = [Col("n", "Stop"), Col("time", "Time"), Col("place", "Place"), Col("street", "Street"),
              Col("fill", "How full when the truck arrives", "bar"), Col("status", "Status", "pill"),
              Col("map", "", "map")]
STATUS_PILL = {"red": ("Already full", "red"), "orange": ("Full by arrival", "orange"),
               "amber": ("Over half", "amber"), "green": ("Fine", "green")}


def route_rows(route: dict) -> list[dict]:
    rows = []
    n = 0
    for s in route["stops"]:
        if s["kind"] == "bin":
            n += 1
            rows.append({"n": str(n), "time": hhmm(s["arrive_ts"]), "place": s["landmark"], "street": s["street"],
                         "fill": s["expected_fill_pct"], "status": STATUS_PILL[s["status"]],
                         "map": (s["lat"], s["lon"]), "kind": "bin"})
        elif s["kind"] == "landfill":
            rows.append({"n": "🚛", "time": hhmm(s["arrive_ts"]), "place": "Turbhe landfill - unload",
                         "street": f"about {duration((s['depart_ts'] - s['arrive_ts']) / 60)}", "fill": None,
                         "status": ("Unload", "blue"), "map": (s["lat"], s["lon"]), "kind": "landfill"})
        else:
            rows.append({"n": "🏁", "time": hhmm(s["arrive_ts"]), "place": "Back at the depot", "street": "",
                         "fill": None, "status": None, "map": None, "kind": "depot"})
    return rows

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
        labels = {r["id"]: f"{r['status'].capitalize()} {'re-plan' if r['kind'] == 'replan' else 'plan'} made {when(r['created_at'])}" for r in choices}
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
    who = (f"approved by {p['approved_by']} at {hhmm(p['approved_at'])}" if p.get("approved_by")
           else "waiting for a supervisor to approve")
    st.markdown(f"<span class='ww-badge {badge}'>{p['status'].upper()}</span> &nbsp; "
                f"{'Re-plan' if p['kind'] == 'replan' else 'Plan'} for <b>{today_label(p['plan_date'])}</b> · "
                f"made at {hhmm(p['created_at'])} by {p['created_by']} · {who}", unsafe_allow_html=True)

    n_read, n_comp = _new_since(p)
    if n_read or n_comp:
        st.warning(f"**New information since this plan was made:** {n_read} new reading(s), {n_comp} new "
                   f"complaint(s). " + ("Use **Re-plan from now** if they change priorities."
                                        if shift_now else "Generate the plan again before the shift if needed."),
                   icon=":material/notifications_active:")
    if not p["verifier"]["ok"]:
        st.error("This plan failed the independent check and cannot be approved: "
                 + "; ".join(p["verifier"]["errors"][:3]))
    summary_card(p)
    show_alerts(p["alerts"])
    kpi_row(p)

    st.markdown("##### Routes on the map")
    vehicles = [r["vehicle_id"] for r in p["routes"] if r["totals"]["bins"]]
    names = {r["vehicle_id"]: r["vehicle_name"] for r in p["routes"]}
    sel = st.pills("Show trucks", vehicles, selection_mode="multi", default=vehicles, format_func=names.get,
                   key="truck_pills") or vehicles
    net = roads.network_for(c) if settings.get("map_tiles") == "offline" else None
    fig = maps.plan_map(p, db.places(c), db.bins(c), net, sel, tiles=settings.get("map_tiles", "osm"))
    st.plotly_chart(fig, use_container_width=True, config={"displaylogo": False, "scrollZoom": True})
    st.caption("Each truck has its own colour (line and ring). The dot in the middle shows how full the bin will "
               "be: 🔴 already full · 🟠 full by the time the truck arrives · 🟡 over half · 🟢 fine. "
               "Numbers = stop order. Grey = not in today's plan.")

    hdr, btn = st.columns([3, 1])
    hdr.markdown("##### Each truck's route")
    if btn.download_button("All trucks (PDF)", route_pdf(p, [r for r in p["routes"] if r["vehicle_id"] in sel]),
                           file_name=f"route_sheets_{p['plan_date']}.pdf", mime="application/pdf",
                           key=f"sheets_all_{p['id']}", icon=":material/picture_as_pdf:"):
        log("route_sheet_downloaded", {"plan_id": p["id"], "vehicle": "all"})
    for r in p["routes"]:
        if r["vehicle_id"] not in sel:
            continue
        t = r["totals"]
        stops = route_rows(r)
        full = sum(1 for x in stops if x["kind"] == "bin" and x["status"][1] in ("red", "orange"))
        title = (f"**{r['vehicle_name']}** · {t['bins']} stops · {t['landfill_trips']} landfill trip(s) · "
                 f"{t['distance_km']:.0f} km · {hhmm(r['start_ts'])} to {hhmm(t['end_ts'])}")
        with st.expander(title, expanded=False):
            st.markdown(f"{full} bin(s) on this route will be full when the truck arrives."
                        + (f" This truck also helps another area at {t['off_beat_bins']} stop(s)."
                           if t["off_beat_bins"] else ""))
            table(stops, ROUTE_COLS, strong=lambda x: x["kind"] != "bin")
            if st.download_button("Route sheet (PDF)", route_pdf(p, [r]),
                                  file_name=f"route_sheet_{p['plan_date']}_{r['vehicle_name'].replace(' ', '_')}.pdf",
                                  mime="application/pdf", key=f"sheet_{p['id']}_{r['vehicle_id']}",
                                  icon=":material/picture_as_pdf:"):
                log("route_sheet_downloaded", {"plan_id": p["id"], "vehicle": r["vehicle_id"]})
    if p["unserved"]:
        due = [u for u in p["unserved"] if u["due"]]
        with st.expander(f"Bins not in today's plan ({len(p['unserved'])}" + (f", {len(due)} due" if due else "") + ")"):
            bins = {b["id"]: b for b in db.bins(c, active_only=False)}
            table([{"place": u["landmark"], "street": bins.get(u["bin_id"], {}).get("street", ""),
                    "why": ("Due, but no truck has space or time - send an extra trip" if u["due"]
                            else "Not due and not full enough today"),
                    "map": (bins[u["bin_id"]]["lat"], bins[u["bin_id"]]["lon"]) if u["bin_id"] in bins else None}
                   for u in p["unserved"]],
                  [Col("place", "Place"), Col("street", "Street"), Col("why", "Why"), Col("map", "", "map")])
    with st.expander("How this plan was checked"):
        v = p["verifier"]
        st.markdown(f"- **Independent check: {'passed ✅' if v['ok'] else 'FAILED ❌'}.** A separate program re-checked "
                    f"all {v['routes_checked']} routes and {v['bins_checked']} stops: arrival times, truck capacity, "
                    f"the final landfill unload, return before the end of the shift, no bin visited twice, every due "
                    f"bin served or explained.")
        for e_ in v["errors"]:
            st.markdown(f"  - ❌ {e_}")
        s = p["solver"]
        st.markdown(f"- **Route search:** WasteWise tried {s['iterations']:,} variations of the routes in "
                    f"{s['seconds']:.0f} seconds and kept the best one.")
        st.markdown(f"- **Fill forecast:** {'AI forecaster' if 'ML' in p['forecast_source'] else 'learned daily pattern'}"
                    + (" (tree model run on the IBM Telum AI accelerator)" if "Telum" in p["forecast_source"] else "")
                    + ".")
        if p.get("bad_sensors"):
            bn = {b["id"]: b["landmark"] for b in db.bins(c, active_only=False)}
            st.markdown("- **Sensors not trusted today** (faulty or offline - their manual readings are used): "
                        + ", ".join(bn.get(x, x) for x in p["bad_sensors"]))
    if p["kind"] in ("daily", "replan"):
        evening_section(p)


# ------------------------------------------------------------------ evening round
@st.cache_data(ttl=300, show_spinner="Checking which bins fill up again before tomorrow ...")
def _assess(plan_id: int, version: int) -> dict:
    c = db.connect()
    return evening.assess(c, planner.get(c, plan_id))


def evening_section(main: dict) -> None:
    c = conn()
    st.divider()
    st.markdown("### Evening round")
    ev = planner.current(c, main["plan_date"], round_="extra")
    a = _assess(main["id"], db.data_version(c))
    by_id = {b["id"]: b for b in db.bins(c)}
    if ev is None:
        if a["candidates"]:
            first = a["candidates"][0]
            with st.container(border=True):
                st.markdown(
                    f"🟠 **AI recommends an evening round.** {len(a['candidates'])} bin(s) will fill up again after "
                    f"the morning round and overflow before tomorrow's round - about "
                    f"**{duration(a['overflow_min_without'])}** of overflowing bins in total, mostly overnight. "
                    f"The first one is full at **{when(first['full_at'])}** ({first['landmark']}).")
                st.markdown(f"**Best time: {hhmm(a['suggested_start_ts'])} to {hhmm(a['suggested_end_ts'])}** - "
                            f"leaves about {duration(a['overflow_min_with'])} of overflow (instead of "
                            f"{duration(a['overflow_min_without'])}). Earlier, bins would fill up again overnight; "
                            f"later, they would overflow first.")
                table([{"place": x["landmark"], "street": x["street"], "full": when(x["full_at"]),
                        "over": f"about {duration(x['overflow_min'])}",
                        "map": (by_id[x["bin_id"]]["lat"], by_id[x["bin_id"]]["lon"]) if x["bin_id"] in by_id else None}
                       for x in a["candidates"]],
                      [Col("place", "Place"), Col("street", "Street"), Col("full", "Full again at"),
                       Col("over", "Overflowing before tomorrow's round"), Col("map", "", "map")])
                if can("generate_plan") and st.button("Plan the evening round at the best time", type="primary",
                                                      key="ev_suggested"):
                    _make_evening(main, a["suggested_start_ts"], a["suggested_end_ts"],
                                  [x["bin_id"] for x in a["candidates"]], a)
        else:
            st.success("No evening round needed: no bin is expected to overflow before tomorrow's round.",
                       icon=":material/check_circle:")
        if can("generate_plan"):
            with st.expander("Plan an evening round for a time and bins you choose"):
                day = datetime.strptime(main["plan_date"], "%Y-%m-%d").date()
                default = a["suggested_start_ts"] or int(datetime(day.year, day.month, day.day, 16, 0,
                                                                  tzinfo=config.IST).timestamp())
                c1, c2 = st.columns(2)
                t = c1.time_input("Start at", value=datetime.fromtimestamp(default, config.IST).time(), step=900,
                                  key="ev_time")
                hours = c2.select_slider("For", [1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0], value=3.0, key="ev_hours",
                                         format_func=lambda h: f"{h:g} hours")
                chosen = st.multiselect("Bins to collect", list(by_id),
                                        default=[x["bin_id"] for x in a["candidates"] if x["bin_id"] in by_id],
                                        format_func=lambda x: f"{by_id[x]['landmark']} - {by_id[x]['street'] or ''}",
                                        key="ev_bins")
                if st.button("Plan this round", key="ev_manual", disabled=not chosen):
                    start = int(datetime.combine(day, t, tzinfo=config.IST).timestamp())
                    _make_evening(main, start, int(start + hours * 3600), chosen, a)
        return

    badge = "ww-approved" if ev["status"] == "approved" else "ww-draft"
    who = (f"approved by {ev['approved_by']} at {hhmm(ev['approved_at'])}" if ev.get("approved_by")
           else "waiting for a supervisor to approve")
    st.markdown(f"<span class='ww-badge {badge}'>{ev['status'].upper()}</span> &nbsp; Evening round "
                f"{hhmm(ev['start_ts'])} to {hhmm(ev['shift_end_ts'])} · {who}", unsafe_allow_html=True)
    k = ev["kpis"]
    st.markdown(f"**{k['bins_served']} bin(s)** will be collected again"
                + (f" - **{k['due_left_out']} do not fit**, choose a longer slot." if k["due_left_out"] else ".")
                + f" Last truck back by **{hhmm(k['finish_ts'])}**.")
    b1, b2, _ = st.columns([1, 1.2, 3])
    if ev["status"] == "draft" and can("approve_plan"):
        if b1.button("Approve evening round", type="primary", disabled=not ev["verifier"]["ok"], key="ev_approve"):
            planner.approve(c, ev["id"], user()["username"])
            mark_seen()
            st.rerun()
    if can("generate_plan") and b2.button("Plan it again", key="ev_redo"):
        with db.tx(c):
            c.execute("UPDATE plans SET status='superseded' WHERE id=?", (ev["id"],))
            db.bump_version(c)
        mark_seen()
        st.rerun()
    used = [r for r in ev["routes"] if r["totals"]["bins"]]
    st.plotly_chart(maps.plan_map(ev, db.places(c), db.bins(c), height=420,
                                  tiles=db.get_settings(c).get("map_tiles", "osm")),
                    use_container_width=True, config={"displaylogo": False, "scrollZoom": True})
    for r in used:
        t_ = r["totals"]
        with st.expander(f"**{r['vehicle_name']}** · {t_['bins']} stops · {hhmm(r['start_ts'])} to {hhmm(t_['end_ts'])}"):
            table(route_rows(r), ROUTE_COLS, strong=lambda x: x["kind"] != "bin")
    if used and st.download_button("Evening route sheets (PDF)", route_pdf(ev, used),
                                   file_name=f"evening_route_sheets_{ev['plan_date']}.pdf", mime="application/pdf",
                                   key=f"ev_pdf_{ev['id']}", icon=":material/picture_as_pdf:"):
        log("route_sheet_downloaded", {"plan_id": ev["id"], "vehicle": "evening"})


def _make_evening(main: dict, start: int, end: int, bins: list[str], a: dict) -> None:
    c = conn()
    try:
        with st.spinner("Planning the evening round ..."):
            p = evening.plan_round(c, user()["username"], main, start, end, bins, assessment=a)
        planner.save(c, p, user()["username"])
        mark_seen()
        st.rerun()
    except planner.PlanError as exc:
        st.error(str(exc))



def page() -> None:
    live_refresh()
    c = conn()
    day = planner.plan_day(c)
    st.title("Today's plan")
    synthetic_notice()
    tabs = st.tabs(["Plan", "Progress", "Live alerts", "Morning briefing & crew messages", "Crew phone view"])
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
