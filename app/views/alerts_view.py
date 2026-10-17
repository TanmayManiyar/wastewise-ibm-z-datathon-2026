"""Live alerts (refreshes automatically) + administrator demonstration tools."""
from __future__ import annotations

import streamlit as st

from wastewise import complaints, db, live

from ui.common import LEVEL_ICON, can, conn, hhmm, mark_seen, user


@st.cache_resource
def feed_thread() -> live.FeedThread:
    t = live.FeedThread(interval=120)
    t.start()
    return t


def tab(day) -> None:
    c = conn()
    by_id = {b["id"]: b for b in db.bins(c)}

    @st.fragment(run_every=10)
    def _list():
        items = live.alerts(c)
        st.caption(f"Updated {hhmm(db.now())} · refreshes by itself every 10 seconds · {len(items)} alert(s)")
        if not items:
            st.success("All quiet.", icon=":material/check_circle:")
        for a in items:
            fn = {"critical": st.error, "warning": st.warning}.get(a["level"], st.info)
            fn(f"**{hhmm(a['ts'])} · {a['text']}**  \nWhat to do: {a['action']}", icon=LEVEL_ICON[a["level"]])
            if a["kind"] == "complaint_new" and can("complaints"):
                cols = st.columns([3, 1])
                opts = a["candidates"] or list(by_id)
                pick = cols[0].selectbox("Which bin?", opts, index=None, key=f"cn_{a['id']}",
                                         placeholder="choose the bin (nothing is pre-selected)",
                                         format_func=lambda x: f"{x} {by_id[x]['landmark']}")
                if cols[1].button("Confirm", key=f"cnb_{a['id']}", disabled=pick is None, type="primary"):
                    complaints.confirm(c, a["id"], pick, user()["username"])
                    st.session_state.highlight_bin = pick
                    mark_seen()
                    st.rerun()

    _list()

    if can("demo_tools"):
        st.divider()
        st.subheader("Demonstration tools (administrator only)")
        st.caption("Everything created here is labelled synthetic / TEST EVENT in the database and on screen.")
        t = feed_thread()
        on = bool(db.get_settings(c).get("demo_feed"))
        new = st.toggle("Live demonstration feed (synthetic sensor readings every 2 minutes)", value=on)
        if new != on:
            with db.tx(c):
                db.set_setting(c, "demo_feed", new)
                from wastewise import audit
                audit.log(c, user()["username"], "demo_feed", {"on": new})
            st.rerun()
        if t.last_run:
            st.caption(f"Feed last added {t.last_count} readings at {hhmm(t.last_run)}.")
        cols = st.columns([2, 2, 1])
        kind = cols[0].selectbox("Test event", list(live.TEST_EVENTS), format_func=live.TEST_EVENTS.get)
        bid = cols[1].selectbox("At bin", list(by_id), index=None, placeholder="choose a bin",
                                format_func=lambda x: f"{x} {by_id[x]['landmark']}")
        if cols[2].button("Inject", disabled=bid is None):
            desc = live.inject(c, kind, bid, user()["username"])
            mark_seen()
            st.success(f"Injected: {desc}")
