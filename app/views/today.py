"""Today's plan: Plan · Progress · Live alerts · Briefing & driver messages · Crew phone view."""
from __future__ import annotations

import streamlit as st

from datetime import datetime

from wastewise import config, db, evening, fmt, jobs, pdfs, planner, roads

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


def _main_key(day) -> str:
    return f"main:{day if isinstance(day, str) else day.isoformat()}"


def _extra_key(day) -> str:
    return f"extra:{day if isinstance(day, str) else day.isoformat()}"


def _generate(kind: str, day, base: dict | None = None) -> None:
    """Start making the plan in the background; job_panel() shows the progress."""
    name = user()["username"]
    key = _main_key(day)
    jobs.start(key, "re-plan" if kind == "replan" else "plan", name,
               lambda c, progress: planner.generate(c, name, kind=kind, replan_of=base, progress=progress))
    st.session_state[f"_watch_{key}"] = True


def job_panel(key: str) -> None:
    """Live progress of a plan being made in the background. The rest of the page stays usable."""
    j = jobs.get(key)
    watching = st.session_state.get(f"_watch_{key}")
    if j is None or (j.status != "running" and not watching):
        return

    @st.fragment(run_every=1)
    def _panel():
        j = jobs.get(key)
        if j.status == "running":
            with st.container(border=True):
                st.markdown(f"**Making the {j.label}** (started by {j.user} at {hhmm(j.started)}). You can keep "
                            "working on the other tabs - the plan appears here when it is ready.")
                text = jobs.PHASE_TEXT.get(j.phase, j.phase)
                if j.phase in ("search", "check") and j.iterations:
                    text += f" · {j.iterations:,} route variations tried"
                    if j.best_km is not None:
                        text += f" · best so far {fmt.km(j.best_km)}"
                st.progress(j.fraction(), text=text + f" · {duration(j.elapsed / 60) if j.elapsed >= 60 else f'{j.elapsed:.0f} s'}")
            return
        if st.session_state.pop(f"_watch_{key}", None):
            if j.status == "done":
                if key.startswith("main:"):
                    st.session_state.plan_view = j.plan_id
                st.session_state["_plan_toast"] = f"The {j.label} is ready (draft). Check it, then approve."
            else:
                st.session_state["_plan_error"] = j.error
            mark_seen()
            st.rerun(scope="app")

    _panel()


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
    extra = [s for r in p["routes"] for s in r["stops"] if s["kind"] == "bin" and not s.get("due", True)]
    with st.container(border=True):
        st.markdown(msg)
        if extra:
            st.markdown(f"➕ **{len(extra)} of these bins are not due today** (collected every 2 days, or not on "
                        "Sundays) but will be nearly full by the next round, so WasteWise added them: "
                        + ", ".join(f"{s['landmark']} ({fmt.fill(s['expected_fill_pct'])})" for s in extra[:4])
                        + (f" and {len(extra) - 4} more" if len(extra) > 4 else "") + ".")


def kpi_row(p: dict) -> None:
    k, b = p["kpis"], p["baseline"]
    over_h, base_h = k["expected_overflow_min"] / 60, b["expected_overflow_min"] / 60
    cols = st.columns(4)
    cols[0].metric("Waste to collect", litres(k["litres"]), f"{k['landfill_trips']} landfill trips", delta_color="off")
    cols[1].metric("Driving", fmt.km(k["distance_km"]), change(k["distance_km"], b["distance_km"], "km"),
                   delta_color="inverse")
    cols[2].metric("Diesel / CO₂", f"{litres(k['fuel_l'])} · {k['co2_kg']:,.0f} kg CO₂",
                   change(k["co2_kg"], b["co2_kg"], "kg CO₂"), delta_color="inverse")
    cols[3].metric("Time bins spend overflowing", f"about {duration(k['expected_overflow_min'])}",
                   (f"{duration(abs(k['expected_overflow_min'] - b['expected_overflow_min']))} "
                    f"{'less' if over_h <= base_h else 'more'} than the old routine"), delta_color="inverse",
                   help="Expected total time, over all bins, that a bin is full before its truck arrives.")
    st.caption("'Old routine' = each truck simply drives to the nearest waiting bin, on the same day and data"
               + (" (demonstration data)." if p.get("label") else "."))


def handled_since(p: dict) -> dict[str, str]:
    """Bins the crews have dealt with since the plan was made (collected, missing, could not reach)."""
    words = {"collected": "collected", "bin_missing": "reported missing", "could_not_reach": "could not be reached"}
    out = {}
    for r in conn().execute("SELECT bin_id, kind FROM events WHERE ts>=? AND bin_id IS NOT NULL AND kind IN"
                            " ('collected','bin_missing','could_not_reach') ORDER BY ts", (p["created_at"],)):
        out[r[0]] = words[r[1]]
    return out


def show_alerts(alerts: list[dict], made_at: int | None = None, handled: dict[str, str] | None = None) -> None:
    """What the plan expected when it was made, minus what the crews have handled since. What is happening
    now is the Live alerts tab (the single 'now' list)."""
    handled = handled or {}
    names = {b["id"]: b["landmark"] for b in db.bins(conn(), active_only=False)}
    kept, done = [], 0
    for a in alerts:
        if a.get("bins"):
            left = [b for b in a["bins"] if b not in handled]
            if not left:
                done += 1
                continue
            if len(left) < len(a["bins"]):
                a = dict(a, bins=left, handled_n=len(a["bins"]) - len(left))
        kept.append(a)
    important = [a for a in kept if a["level"] in ("critical", "warning")]
    info = [a for a in kept if a["level"] not in ("critical", "warning")]

    def line(a):
        icon = "🔴" if a["level"] == "critical" else "🟠" if a["level"] == "warning" else "ℹ️"
        places = ""
        if a.get("bins"):
            ps = [names.get(b, b) for b in a["bins"][:4]]
            places = " · " + ", ".join(ps) + (f" and {len(a['bins']) - 4} more" if len(a["bins"]) > 4 else "")
        if a.get("handled_n"):
            places += f" ({a['handled_n']} more already handled by the crews)"
        return f"{icon} **{a['text']}**{places}  \n&nbsp;&nbsp;&nbsp;&nbsp;→ {a['action']}"

    with st.container(border=True):
        h, b = st.columns([2.4, 1])
        h.markdown("##### When the plan was made" + (f" ({hhmm(made_at)})" if made_at else ""))
        b.button("Live alerts: what is happening now", key="to_live", on_click=_open_live_alerts,
                 icon=":material/notifications_active:", use_container_width=True)
        st.caption("What WasteWise expected when it made this plan. Items the crews have handled since are left out"
                   + (f" ({done} so far)" if done else "") + ". New complaints, sensor problems and crew reports "
                   "appear under **Live alerts**.")
        if not important:
            st.markdown("✅ Nothing needed attention.")
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
              Col("why", "Why today"), Col("map", "", "map")]
STATUS_PILL = {k: (v, k) for k, v in fmt.PLAN_PILL.items()}


def route_rows(route: dict) -> list[dict]:
    rows = []
    n = 0
    for s in route["stops"]:
        if s["kind"] == "bin":
            n += 1
            rows.append({"n": str(n), "time": hhmm(s["arrive_ts"]), "place": s["landmark"], "street": s["street"],
                         "fill": (s["expected_fill_pct"], s["status"]), "status": STATUS_PILL[s["status"]],
                         "why": "due today" if s.get("due", True) else "not due, but nearly full",
                         "map": (s["lat"], s["lon"]), "kind": "bin"})
        elif s["kind"] == "landfill":
            rows.append({"n": "🚛", "time": hhmm(s["arrive_ts"]), "place": "Turbhe landfill - unload",
                         "street": f"about {duration((s['depart_ts'] - s['arrive_ts']) / 60)}", "fill": None,
                         "status": ("Unload", "blue"), "map": (s["lat"], s["lon"]), "kind": "landfill"})
        else:
            rows.append({"n": "🏁", "time": hhmm(s["arrive_ts"]), "place": "Back at the depot", "street": "",
                         "fill": None, "status": None, "map": None, "kind": "depot"})
    return rows

def time_hint(day) -> dict:
    """Which actions are possible right now, in one sentence at the top of the plan, and why the other
    buttons are switched off."""
    c = conn()
    s = db.get_settings(c)
    now = db.now()
    start, end = planner.shift_window(s, day)
    _, t_end = planner.shift_window(s, planner.today(now))
    if now < start:
        phase = "before"
        text = (f"🕔 **Before the shift.** The shift on {fmt.day_name(day)} starts at **{hhmm(start)}**. Now you can "
                "generate the plan, check it, approve it, then print route sheets and send the crew messages. "
                f"Re-planning opens at {hhmm(start)}, when the trucks leave.")
    elif now < end:
        phase = "during"
        text = (f"🚛 **Shift under way** ({hhmm(start)} to {hhmm(end)}). Now you can follow **Progress** and "
                "**Live alerts**, and use **Re-plan from now** if something changes - it starts each truck from "
                f"where it is. A plan for the next day can be made after {hhmm(end)}.")
    if now >= end or day > planner.today(now):
        phase = "after"
        text = (f"🌙 **Today's shift is over** (ended {hhmm(t_end)}). Now you can plan the next shift, "
                f"**{fmt.day_name(day)} at {hhmm(start)}**, and plan an evening round for today (below) if bins "
                "will overflow before then.")
    st.info(text)
    return {"phase": phase, "start": start, "end": end}


def plan_tab(day) -> None:
    hint = time_hint(day)
    _plan_body(day, hint)
    today_ = planner.today()
    main = planner.current(conn(), today_)
    if main and main["kind"] in ("daily", "replan"):
        evening_section(main, today=True)


def _plan_body(day, hint: dict) -> None:
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

    busy = jobs.running(_main_key(day))
    if st.session_state.get("_plan_toast"):
        st.toast(st.session_state.pop("_plan_toast"), icon=":material/check_circle:")
    if st.session_state.get("_plan_error"):
        st.error(st.session_state.pop("_plan_error"))
    # why each switched-off button is off, in plain words (shown as tooltip and below the buttons)
    why_gen = ("A plan is being made right now." if busy else
               f"Not during the shift (until {hhmm(hint['end'])}) - use Re-plan from now." if shift_now else None)
    why_approve = None if not p or p["verifier"]["ok"] else "This plan failed the independent check."
    why_replan = ("A plan is being made right now." if busy else
                  f"Opens when the shift starts at {hhmm(hint['start'])}." if hint["phase"] == "before" else
                  "The shift is over." if hint["phase"] == "after" else
                  "Needs an approved plan for today." if not approved else None)
    bar_ = st.container(horizontal=True)       # buttons keep their natural width (no cut-off labels)
    b1 = b2 = b3 = bar_
    off = []
    if can("generate_plan"):
        if b1.button("Generate plan", type="primary" if p is None else "secondary", disabled=bool(why_gen),
                     help=why_gen or "Plan the next shift from the latest data."):
            _generate("daily", day)
            st.rerun()
        if why_gen and not busy:
            off.append(f"**Generate plan** is off: {why_gen}")
    if p and can("approve_plan") and p["status"] == "draft":
        if b2.button("Approve plan", type="primary", disabled=bool(why_approve), help=why_approve):
            planner.approve(c, p["id"], user()["username"])
            mark_seen()
            st.rerun()
        if why_approve:
            off.append(f"**Approve plan** is off: {why_approve}")
    if can("replan"):
        if b3.button("Re-plan from now", disabled=bool(why_replan),
                     help=why_replan or "Starts each truck from where it is now, with what it carries."):
            _generate("replan", day, approved)
            st.rerun()
        if why_replan and not busy:
            off.append(f"**Re-plan from now** is off: {why_replan}")
    if off:
        st.caption(" · ".join(off))
    job_panel(_main_key(day))

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
    show_alerts(p["alerts"], p["created_at"], handled_since(p))
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
               f"be: {fmt.legend(fmt.PLAN_WORDS)}. Numbers = stop order. Grey = not in today's plan.")

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
                 f"{fmt.km(t['distance_km'])} · {hhmm(r['start_ts'])} to {hhmm(t['end_ts'])}")
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
                            else "Not collected on this weekday, and not full enough" if u.get("rest_day")
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


# ------------------------------------------------------------------ evening round
@st.cache_data(ttl=300, show_spinner="Checking which bins fill up again before tomorrow ...")
def _assess(plan_id: int, version: int) -> dict:
    c = db.connect()
    return evening.assess(c, planner.get(c, plan_id))


def evening_section(main: dict, today: bool = False) -> None:
    c = conn()
    st.divider()
    st.markdown(f"### Evening round {'today' if today else ''} · {fmt.day_name(main['plan_date'])}")
    job_panel(_extra_key(main["plan_date"]))
    ev_busy = bool(jobs.running(_extra_key(main["plan_date"])))
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
                                                      key="ev_suggested", disabled=ev_busy):
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
                if st.button("Plan this round", key="ev_manual", disabled=not chosen or ev_busy):
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
    name = user()["username"]
    key = _extra_key(main["plan_date"])
    jobs.start(key, "evening round", name,
               lambda c, progress: evening.plan_round(c, name, main, start, end, bins, assessment=a, progress=progress))
    st.session_state[f"_watch_{key}"] = True
    st.rerun()



def page() -> None:
    live_refresh()
    c = conn()
    day = planner.plan_day(c)
    st.title("Today's plan")
    synthetic_notice()
    now_day = planner.today()
    tabs = st.tabs(TABS, key="today_tab", on_change="rerun")
    with tabs[0]:
        plan_tab(day)
    from views import progress, alerts_view, briefing, crew
    with tabs[1]:
        progress.tab(now_day)
    with tabs[2]:
        alerts_view.tab(now_day)
    with tabs[3]:
        briefing.tab(day)
    with tabs[4]:
        crew.preview(now_day)


TABS = ["Plan", "Progress", "Live alerts", "Morning briefing & crew messages", "Crew phone view"]


def _open_live_alerts() -> None:
    st.session_state["today_tab"] = "Live alerts"
