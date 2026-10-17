"""Settings > Roads: road closures, and the landfill gate hours and queue."""
from __future__ import annotations

from datetime import datetime, timedelta

import streamlit as st

from wastewise import audit, closures, config, db, fmt

from ui import street_pick
from ui.common import can, conn, mark_seen, user
from ui.tables import Col, table


@st.cache_data(show_spinner=False)
def _streets() -> dict[str, dict]:
    return {k: {"lat": v["lat"], "lon": v["lon"], "line": v["line"], "segments": v["segments"]}
            for k, v in closures.street_index().items()}


def _at(d, t) -> int:
    return int(datetime.combine(d, t, tzinfo=config.IST).timestamp())


def tab() -> None:
    c = conn()
    st.markdown("**Closed roads** - trucks are routed around them until the time you set. Bins that can only be "
                "reached through a closed road are left out of the plan and listed with the reason.")
    cur = closures.upcoming(c)
    blocked = closures.unreachable_bins(c)
    names = {b["id"]: b["landmark"] for b in db.bins(c, active_only=False)}
    if cur:
        table([{"street": r["street"], "from": fmt.when(r["from_ts"]), "until": fmt.when(r["until_ts"]),
                "why": r["reason"] or "", "now": ("Closed now", "red") if r["from_ts"] <= db.now() else ("Later", "amber"),
                "bins": ", ".join(names.get(b, b) for b, cl in blocked.items() if cl["id"] == r["id"]) or "none"}
               for r in cur],
              [Col("street", "Street"), Col("from", "From"), Col("until", "Until"), Col("why", "Reason"),
               Col("now", "", "pill"), Col("bins", "Bins that cannot be reached")])
    else:
        st.caption("No road is closed.")
    if not can("edit_bins"):
        return
    streets = _streets()
    st.caption("Choose the street from the list, or tap it on the street map (red = closed now, blue = chosen).")
    closed_now = {r["street"] for r in cur if r["from_ts"] <= db.now()}
    tapped = street_pick.pick(streets, st.session_state.get("close_street"), closed_now)
    if tapped in streets and tapped != st.session_state.get("close_street"):
        st.session_state["close_street"] = tapped
        st.rerun()
    with st.form("close_road"):
        opts = sorted(streets)
        street = st.selectbox("Street", opts, index=opts.index(st.session_state["close_street"])
                              if st.session_state.get("close_street") in opts else None,
                              placeholder="choose the street")
        a, b, d = st.columns(3)
        now = datetime.now(config.IST)
        until_d = a.date_input("Closed until (date)", value=(now + timedelta(days=1)).date())
        until_t = b.time_input("Closed until (time)", value=datetime.strptime("18:00", "%H:%M").time(), step=1800)
        from_now = d.checkbox("Closed from now", value=True)
        reason = st.text_input("Reason (optional)", placeholder="road works, pandal, flooding ...")
        if st.form_submit_button("Close this road", type="primary") and street:
            try:
                closures.add(c, street, _at(until_d, until_t), reason, user()["username"],
                             None if from_now else _at(now.date() + timedelta(days=1), datetime.min.time()))
                st.session_state.pop("close_street", None)
                mark_seen()
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
    if cur:
        with st.form("reopen"):
            rid = st.selectbox("Open a road again", [r["id"] for r in cur], index=None, placeholder="choose",
                               format_func=lambda i: next(f"{r['street']} (until {fmt.when(r['until_ts'])})"
                                                          for r in cur if r["id"] == i))
            if st.form_submit_button("Open again") and rid:
                closures.lift(c, rid, user()["username"])
                mark_seen()
                st.rerun()

    st.markdown("**Landfill gate** (Turbhe) - routes only plan unloading while the gate is open; a truck that arrives "
                "early waits. The queue time is added to every unloading.")
    s = db.get_settings(c)
    with st.form("landfill"):
        a, b, d = st.columns(3)
        o = a.text_input("Opens (HH:MM)", s.get("landfill_open", "06:00"))
        cl = b.text_input("Closes (HH:MM, 24:00 = midnight)", s.get("landfill_close", "22:00"))
        q = d.number_input("Usual queue (minutes)", 0.0, 180.0, float(s.get("landfill_queue_min", 10)), 5.0)
        st.caption("Demonstration values - confirm the real gate hours with NMMC. 00:00 to 24:00 = always open.")
        if st.form_submit_button("Save landfill hours") and can("settings"):
            try:
                for v in (o, cl):
                    hh, mm = map(int, v.split(":"))
                    assert 0 <= hh <= 24 and 0 <= mm < 60
            except (ValueError, AssertionError):
                st.error("Times must look like 06:00")
                return
            with db.tx(c):
                for k, v in (("landfill_open", o), ("landfill_close", cl), ("landfill_queue_min", float(q))):
                    db.set_setting(c, k, v)
                audit.log(c, user()["username"], "landfill_hours_changed", {"open": o, "close": cl, "queue_min": q})
                db.bump_version(c)
            st.toast("Saved: landfill gate hours")
