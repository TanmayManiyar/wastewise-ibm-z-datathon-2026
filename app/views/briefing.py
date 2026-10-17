"""Morning briefing & crew messages.

Purpose: turn the approved plan into (1) a short briefing the supervisor reads out at the morning
meeting and (2) a ready-to-send message for each truck's crew in English, Hindi or Marathi, for
crews who do not use the app. Every figure comes straight from the plan. If the local IBM Granite
model is running, it can reword the text; its wording is only shown if every figure checks out.
"""
from __future__ import annotations

import urllib.parse

import streamlit as st

from wastewise import audit, llm, planner

from ui.common import conn, today_label, user


def _share(text: str, key: str) -> None:
    st.link_button("Share on WhatsApp", "https://wa.me/?" + urllib.parse.urlencode({"text": text}),
                   icon=":material/share:", help="Opens WhatsApp with the message filled in - you choose the "
                                                  "contact and press Send.")


def tab(day) -> None:
    c = conn()
    p = planner.current(c, day)
    if not p:
        st.info("Generate a plan first.")
        return
    st.caption("What this is for: a short briefing to read out at the morning meeting, and a ready-made message for "
               "each truck's crew in their language - for crews who do not use the app. Every figure comes "
               "straight from the plan.")

    st.markdown(f"#### Morning briefing · {today_label(p['plan_date'])}")
    key = f"brief_{p['id']}"
    res = st.session_state.get(key) or llm.briefing(c, p, use_llm=False)
    lines = res["text"].splitlines()
    with st.container(border=True):
        st.markdown(f"**{lines[0]}**" if lines else "")
        for line in lines[1:]:
            st.markdown(f"- {line}")
    a, b, _ = st.columns([1, 1.4, 2])
    with a:
        _share(res["text"], key)
    if llm.usable(c, "briefing") and b.button("Let Granite reword it", key="brief_btn"):
        with st.spinner("Writing ..."):
            st.session_state[key] = llm.briefing(c, p, use_llm=True)
        audit.log(c, user()["username"], "briefing_written", {"plan_id": p["id"], "source": st.session_state[key]["source"]})
        st.rerun()
    _note(res)

    st.markdown("#### Messages for the crews")
    names = {r["vehicle_id"]: r["vehicle_name"] for r in p["routes"] if r["totals"]["bins"]}
    if not names:
        return
    a, b = st.columns(2)
    vid = a.selectbox("Truck", list(names), format_func=names.get, key="dm_truck")
    lang = b.segmented_control("Language", list(llm.LANG_NAMES), default="en", key="dm_lang",
                               format_func={"en": "English", "hi": "हिंदी", "mr": "मराठी"}.get) or "en"
    mkey = f"dm_{p['id']}_{vid}_{lang}"
    msg = st.session_state.get(mkey) or llm.driver_message(c, p, vid, lang, use_llm=False)
    with st.container(border=True):
        st.markdown(msg["text"])
    a, b, _ = st.columns([1, 1.4, 2])
    with a:
        _share(msg["text"], mkey)
    if llm.usable(c, f"driver_{lang}") and b.button("Let Granite reword it", key="dm_btn"):
        with st.spinner("Writing ..."):
            st.session_state[mkey] = llm.driver_message(c, p, vid, lang, use_llm=True)
        audit.log(c, user()["username"], "driver_message_written",
                  {"plan_id": p["id"], "vehicle": vid, "lang": lang, "source": st.session_state[mkey]["source"]})
        st.rerun()
    _note(msg)


def _note(res: dict) -> None:
    if res["source"] == "Granite":
        st.caption("Reworded by IBM Granite on this server - all figures checked against the plan.")
    elif res.get("error"):
        st.caption("Granite's wording was not used because it did not match the plan; this text comes straight "
                   "from the plan.")
