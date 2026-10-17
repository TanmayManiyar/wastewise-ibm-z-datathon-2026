"""Live alerts (refreshes automatically) + administrator demonstration tools."""
from __future__ import annotations

import streamlit as st

from wastewise import complaints, db, live

from ui.common import LEVEL_ICON, can, conn, hhmm, mark_seen, user


def tab(day) -> None:
    c = conn()
    by_id = {b["id"]: b for b in db.bins(c)}

    @st.fragment(run_every=10)
    def _list():
        items = live.alerts(c)
        pred = [a for a in items if a["kind"] == "predicted"]
        rest = [a for a in items if a["kind"] != "predicted"]
        st.caption(f"Updated {hhmm(db.now())} · refreshes by itself every 10 seconds")
        if not items:
            st.success("All quiet - no bin is predicted to overflow before its next collection.",
                       icon=":material/check_circle:")
        if pred:
            from wastewise import outlook_now
            eng = outlook_now.predict(c)["engine"] or "learned pattern (no trained forecaster yet)"
            st.markdown(f"##### Predicted to overflow before the next collection ({len(pred)})")
            st.caption(f"From the forecaster ({eng}), compared with each bin's next planned collection. "
                       "Earliest first. Bins without a sensor use their learned pattern and say so.")

            def show(a):
                fn = st.error if a["level"] == "critical" else st.warning
                fn(f"**{a['text']}**  \nWhat to do: {a['action']}", icon=LEVEL_ICON[a["level"]])
            for a in pred[:5]:
                show(a)
            if len(pred) > 5:
                with st.expander(f"{len(pred) - 5} more predicted"):
                    for a in pred[5:]:
                        show(a)
        if rest:
            st.markdown(f"##### Happening now ({len(rest)})")
        for a in rest:
            fn = {"critical": st.error, "warning": st.warning}.get(a["level"], st.info)
            fn(f"**{hhmm(a['ts'])} · {a['text']}**  \nWhat to do: {a['action']}", icon=LEVEL_ICON[a["level"]])
            if a["kind"] == "complaint_ready" and can("complaints"):
                if st.button(f"Close {complaints.ref(a['id'])}", key=f"close_{a['id']}", icon=":material/task_alt:"):
                    complaints.resolve(c, a["id"], user()["username"], note="closed from Live alerts")
                    mark_seen()
                    st.rerun()
            if a["kind"] == "complaint_new" and can("complaints"):
                cols = st.columns([3, 1])
                opts = a["candidates"] or list(by_id)
                pick = cols[0].selectbox("Which bin?", opts, index=None, key=f"cn_{a['id']}",
                                         placeholder="choose the bin (nothing is pre-selected)",
                                         format_func=lambda x: f"{by_id[x]['landmark']} - {by_id[x]['street'] or ''}")
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
        t = live.demo_daemon()
        on = live.feed_on(c)
        new = st.toggle("Live demonstration feed (synthetic sensor readings every 2 minutes)", value=on,
                        help="On by default in demonstration mode.")
        if new != on:
            with db.tx(c):
                db.set_setting(c, "demo_feed", new)
                from wastewise import audit
                audit.log(c, user()["username"], "demo_feed", {"on": new})
            st.rerun()
        if t.last_run:
            st.caption(f"Feed last added {t.last_count} readings at {hhmm(t.last_run)}.")
        if t.last_roll:
            st.caption(f"The synthetic history was moved forward to now at {hhmm(t.last_roll)} (it does this by "
                       "itself when it is more than 2 hours old).")
        if t.error:
            st.caption(f"Demonstration feed problem: {t.error}")
        cols = st.columns([2, 2, 1])
        kind = cols[0].selectbox("Test event", list(live.TEST_EVENTS), format_func=live.TEST_EVENTS.get)
        bid = cols[1].selectbox("At bin", list(by_id), index=None, placeholder="choose a bin",
                                format_func=lambda x: f"{by_id[x]['landmark']} - {by_id[x]['street'] or ''}")
        if cols[2].button("Inject", disabled=bid is None):
            desc = live.inject(c, kind, bid, user()["username"])
            mark_seen()
            st.success(f"Injected: {desc}")
