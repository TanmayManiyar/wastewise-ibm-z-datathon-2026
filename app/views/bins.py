"""Bins: Status and map · Report a problem (complaints, with the complaint register)."""
from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from wastewise import beats, complaints, db, planner, risk, roads, state

from ui import maps
from ui.common import STATUS_WORDS, can, conn, live_refresh, log, mark_seen, synthetic_notice, user, when


@st.cache_data(ttl=60, show_spinner=False)
def _status(version: int) -> tuple[dict, dict, dict]:
    """Current fill estimate per bin (cached per data version)."""
    c = db.connect()
    now = db.now()
    prof = state.rate_profiles(c, now)
    st_ = state.estimate(c, now, prof)
    fills = {b: s.fill for b, s in st_.items()}
    status = {b: ("red" if f >= 100 else "amber" if f >= 50 else "green") for b, f in fills.items()}
    basis = {b: f"{s.basis} at {when(s.basis_ts)}" for b, s in st_.items()}
    return fills, status, basis


def status_tab() -> None:
    c = conn()
    bins = db.bins(c)
    fills, status, basis = _status(db.data_version(c))
    vehicles = {v["id"]: v for v in db.vehicles(c, active_only=False)}
    bmap = beats.load(c)
    colour = {b: vehicles[v]["color"] for b, v in bmap.items() if v in vehicles}
    a, b_, c_, d = st.columns(4)
    a.metric("Bins", len(bins))
    b_.metric("Full or overflowing now", sum(1 for s in status.values() if s == "red"))
    c_.metric("Over half full", sum(1 for s in status.values() if s == "amber"))
    d.metric("Open complaints", c.execute("SELECT COUNT(*) FROM complaints WHERE status='open'").fetchone()[0])
    s = db.get_settings(c)
    net = roads.network_for(c) if s.get("map_tiles") == "offline" else None
    fig = maps.bins_map(bins, status, fills, db.places(c), colour, net, tiles=s.get("map_tiles", "osm"),
                        highlight=st.session_state.get("highlight_bin"))
    st.plotly_chart(fig, use_container_width=True, config={"displaylogo": False, "scrollZoom": True})
    st.caption("Centre colour = estimated fill now (red full, amber over half, green fine). Ring = the truck "
               "whose area (beat) the bin belongs to.")
    rows = []
    for b in bins:
        rows.append({"Bin": b["id"], "Place": b["landmark"], "Street": b["street"], "Type": b["site_type"],
                     "Capacity (L)": b["capacity_l"], "Priority": b["priority"], "Sensor": "yes" if b["has_sensor"] else "no",
                     "Every (days)": b["collect_every_days"], "Fill now": round(fills.get(b["id"], 0)),
                     "Status": STATUS_WORDS[status[b["id"]]].replace("full at shift start", "full"),
                     "Based on": basis.get(b["id"]), "Area of": bmap.get(b["id"], "-")})
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True,
                 column_config={"Fill now": st.column_config.ProgressColumn("Fill now", min_value=0, max_value=120,
                                                                            format="%d%%")})
    if can("edit_bins"):
        with st.expander("Enter a reading (supervisor visit)"):
            with st.form("manual_reading", clear_on_submit=True):
                bid = st.selectbox("Bin", [b["id"] for b in bins], index=None, placeholder="Choose the bin",
                                   format_func=lambda x: f"{x} {next(b['landmark'] for b in bins if b['id'] == x)}")
                f = st.select_slider("How full is it?", [0, 10, 25, 50, 75, 90, 100], value=50)
                if st.form_submit_button("Save reading") and bid:
                    db.add_reading(c, bid, f, "supervisor", user=user()["username"])
                    log("reading_entered", {"bin_id": bid, "fill": f})
                    mark_seen()
                    st.success(f"Saved: {bid} {f}% full.")


def _index():
    if "place_index" not in st.session_state or st.session_state.get("_pi_v") != len(db.bins(conn())):
        st.session_state.place_index = complaints.PlaceIndex(db.bins(conn()))
        st.session_state._pi_v = len(db.bins(conn()))
    return st.session_state.place_index


def report_tab() -> None:
    c = conn()
    bins = db.bins(c)
    by_id = {b["id"]: b for b in bins}
    if can("complaints"):
        st.write("Type or paste the complaint in plain words - English, हिंदी, मराठी or mixed.")
        text = st.text_area("Complaint", key="complaint_text", height=90,
                            placeholder="e.g. डीमार्ट जुईनगर के पास कूड़ेदान भर गया है")
        reporter = st.text_input("Reported by (optional)", key="complaint_reporter",
                                 placeholder="name / phone / 'citizen call'")
        if st.button("Understand", type="primary", disabled=not text.strip()):
            st.session_state.understood = (text, complaints.understand(text, _index()))
        und_pair = st.session_state.get("understood")
        if und_pair and und_pair[0] == text:
            und = und_pair[1]
            probs = ", ".join(complaints.PROBLEM_WORDS[p] for p in und.problems) or "problem not recognised"
            lang = {"en": "English", "hi": "Hindi", "mr": "Marathi", "mixed": "mixed"}[und.language]
            box = st.container(border=True)
            box.markdown(f"**We understood:** {und.summary()}  \nLanguage: {lang} · Problem: {probs}")
            if und.urgent:
                box.error("Urgent: fire or dead animal - inform the supervisor now and send the nearest crew.",
                          icon=":material/emergency:")
            options = [x.bin_id for x in und.candidates]
            if und.decision == "one":
                q = "Is this the right bin?"
            elif und.decision == "choose":
                q = "Several places match - which bin is it?"
            else:
                q = "We could not recognise the place - choose the bin"
                options = [b["id"] for b in bins]
            choice = box.radio(q, options, index=None, key="complaint_choice",
                               format_func=lambda x: f"{x} · {by_id[x]['landmark']} · {by_id[x]['street']}")
            other = box.selectbox("Or pick another bin", [b["id"] for b in bins], index=None,
                                  placeholder="(only if none above is right)", key="complaint_other",
                                  format_func=lambda x: f"{x} {by_id[x]['landmark']}")
            chosen = other or choice
            if box.button("Confirm and save", type="primary", disabled=chosen is None):
                cid = complaints.record(c, text, chosen, und, user()["username"], reporter or None)
                st.session_state.pop("understood", None)
                st.session_state.highlight_bin = chosen
                mark_seen()
                st.success(f"Complaint #{cid} saved for {chosen}. The bin now shows as full on all screens; "
                           "the control room sees it in Live alerts.")
    st.subheader("Complaint register")
    flt = st.segmented_control("Show", ["new", "open", "resolved", "all"], default="open", key="compl_filter",
                               format_func={"new": "new (place to confirm)", "open": "open", "resolved": "resolved",
                                            "all": "all"}.get) or "open"
    q = "SELECT * FROM complaints" + ("" if flt == "all" else " WHERE status = ?") + " ORDER BY ts DESC LIMIT 200"
    rows = db.rows(c, q, () if flt == "all" else (flt,))
    if not rows:
        st.info("No complaints.")
        return
    df = pd.DataFrame([{"#": r["id"], "When": when(r["ts"]), "Bin": r["bin_id"] or "?",
                        "Place": by_id[r["bin_id"]]["landmark"] if r["bin_id"] in by_id else "-",
                        "Problem": complaints.PROBLEM_WORDS.get(r["problem"], r["problem"]), "Text": r["text"],
                        "Language": r["lang"], "Status": r["status"],
                        "Moved from": r["original_bin_id"] or "", "Demo": "synthetic" if r["synthetic"] else ""}
                       for r in rows])
    st.dataframe(df, hide_index=True, use_container_width=True)
    if can("complaints"):
        open_ids = [r["id"] for r in rows if r["status"] == "open"]
        col1, col2 = st.columns(2)
        with col1.form("resolve"):
            cid = st.selectbox("Mark as resolved", open_ids, index=None, placeholder="complaint #")
            if st.form_submit_button("Resolve") and cid:
                complaints.resolve(c, cid, user()["username"])
                mark_seen()
                st.rerun()
        with col2.form("move"):
            cid = st.selectbox("Move a report to the right bin", [r["id"] for r in rows], index=None,
                               placeholder="complaint #")
            nb = st.selectbox("Right bin", [b["id"] for b in bins], index=None, placeholder="bin",
                              format_func=lambda x: f"{x} {by_id[x]['landmark']}")
            if st.form_submit_button("Move (original kept)") and cid and nb:
                complaints.move(c, cid, nb, user()["username"])
                mark_seen()
                st.rerun()


def page() -> None:
    live_refresh()
    st.title("Bins")
    synthetic_notice()
    t1, t2 = st.tabs(["Status and map", "Report a problem"])
    with t1:
        status_tab()
    with t2:
        report_tab()
