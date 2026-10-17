"""Briefing & driver messages: Granite-written (checked against the plan) or the built-in templates."""
from __future__ import annotations

import streamlit as st

from wastewise import audit, db, llm, planner

from ui.common import conn, user


def tab(day) -> None:
    c = conn()
    p = planner.current(c, day)
    if not p:
        st.info("Generate a plan first.")
        return
    stt = llm.status(c)
    if stt["ok"]:
        st.session_state._llm_label = "Granite"
        st.caption(f"Language model: {stt['model']} on this server. Every figure it writes is checked against the "
                   "plan; if one does not match, the checked template is shown instead.")
    else:
        st.caption(f"Language model not available ({stt['error']}). The built-in templates below are generated "
                   "directly from the plan.")
    use = stt["ok"]

    st.subheader("Pre-shift briefing")
    key = f"brief_{p['id']}"
    if st.button("Write the briefing", type="primary", key="brief_btn"):
        with st.spinner("Writing ..."):
            st.session_state[key] = llm.briefing(c, p, use_llm=use)
        audit.log(c, user()["username"], "briefing_written", {"plan_id": p["id"], "source": st.session_state[key]["source"]})
    res = st.session_state.get(key) or llm.briefing(c, p, use_llm=False)
    _show(res)

    st.subheader("Driver messages")
    names = {r["vehicle_id"]: r["vehicle_name"] for r in p["routes"] if r["totals"]["bins"]}
    if not names:
        return
    a, b = st.columns(2)
    vid = a.selectbox("Truck", list(names), format_func=names.get, key="dm_truck")
    lang = b.segmented_control("Language", list(llm.LANG_NAMES), default="en", key="dm_lang",
                               format_func={"en": "English", "hi": "हिंदी", "mr": "मराठी"}.get) or "en"
    mkey = f"dm_{p['id']}_{vid}_{lang}"
    if st.button("Write the message", key="dm_btn"):
        with st.spinner("Writing ..."):
            st.session_state[mkey] = llm.driver_message(c, p, vid, lang, use_llm=use)
        audit.log(c, user()["username"], "driver_message_written",
                  {"plan_id": p["id"], "vehicle": vid, "lang": lang, "source": st.session_state[mkey]["source"]})
    _show(st.session_state.get(mkey) or llm.driver_message(c, p, vid, lang, use_llm=False))
    st.caption("Copy the message with the button at the top right of the box and send it on the crew's phone.")


def _show(res: dict) -> None:
    st.code(res["text"], language=None, wrap_lines=True)
    src = res["source"]
    if src == "Granite":
        st.caption(f"Written by IBM Granite · check passed ({res['check']['numbers_checked']} figures matched the plan)")
    else:
        st.caption("Built-in template (figures taken directly from the plan)")
    if res.get("error"):
        st.warning(res["error"])
        if res.get("rejected"):
            with st.expander("Rejected text"):
                st.write(res["rejected"])
                st.write("Not in the plan:", res["check"]["unknown_numbers"] + res["check"]["unknown_bins"])
