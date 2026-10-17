"""Bins: Status and map · Report a problem (complaints, with the complaint register)."""
from __future__ import annotations

import json

import streamlit as st

from wastewise import area, beats, complaints, db, fmt, planner, risk, roads, state

from ui import maps
from views.settings import every_text
from ui.tables import Col, table
from ui.common import STATUS_WORDS, can, conn, live_refresh, log, mark_seen, synthetic_notice, user, when


SITE_LABEL = {s.key: s.label for s in area.SITE_TYPES}
STATUS_PILL = {k: (v, k) for k, v in fmt.NOW_PILL.items()}
BASIS = {"sensor": "sensor", "api": "sensor", "supervisor": "supervisor visit", "crew": "crew report",
         "complaint": "complaint", "collected": "emptied", "unknown": "no data yet"}


def _basis_text(basis: str, ts) -> str:
    word = BASIS.get(basis.split(" ")[0], basis)
    return f"{word}, {when(ts)}" if ts else word


def _when_rel(ts: int, now: int) -> str:
    from datetime import datetime
    from wastewise import config
    d, n = datetime.fromtimestamp(ts, config.IST), datetime.fromtimestamp(now, config.IST)
    day = "today" if d.date() == n.date() else "tomorrow" if (d.date() - n.date()).days == 1 else d.strftime("%a")
    return f"{day} {d.strftime('%H:%M')}"


def bin_label(b: dict) -> str:
    return f"{b['landmark']} - {b['street'] or ''}"


@st.cache_data(ttl=60, show_spinner=False)
def _status(version: int) -> tuple[dict, dict, dict, dict]:
    """Current fill estimate per bin (cached per data version)."""
    c = db.connect()
    now = db.now()
    prof = state.rate_profiles(c, now)
    st_ = state.estimate(c, now, prof)
    fills = {b: s.fill for b, s in st_.items()}
    basis = {b: _basis_text(s.basis, s.basis_ts) for b, s in st_.items()}
    full_at, status = {}, {}
    for b, s_ in st_.items():
        soon = s_.fill + state.growth_between(prof[b], now, now + fmt.SOON_HOURS * 3600)
        status[b] = fmt.status_now(s_.fill, soon)
        if s_.fill >= 100:
            full_at[b] = "full now"
            continue
        f, t = s_.fill, now
        for _ in range(48 * 6):          # 10-minute steps, up to 2 days ahead
            f += state.growth_between(prof[b], t, t + 600)
            t += 600
            if f >= 100:
                break
        full_at[b] = _when_rel(t, now) if f >= 100 else "not in the next 2 days"
    return fills, status, basis, full_at


def status_tab() -> None:
    c = conn()
    bins = db.bins(c)
    fills, status, basis, full_at = _status(db.data_version(c))
    vehicles = {v["id"]: v for v in db.vehicles(c, active_only=False)}
    bmap = beats.load(c)
    colour = {b: vehicles[v]["color"] for b, v in bmap.items() if v in vehicles}
    a, b_, c_, d = st.columns(4)
    a.metric("Bins", len(bins))
    b_.metric("Full or overflowing now", sum(1 for s in status.values() if s == "red"))
    c_.metric("Full within 3 hours", sum(1 for s in status.values() if s == "orange"))
    d.metric("Open complaints", c.execute("SELECT COUNT(*) FROM complaints WHERE status='open'").fetchone()[0])
    s = db.get_settings(c)
    net = roads.network_for(c) if s.get("map_tiles") == "offline" else None
    fig = maps.bins_map(bins, status, fills, db.places(c), colour, net, tiles=s.get("map_tiles", "osm"),
                        highlight=st.session_state.get("highlight_bin"))
    st.plotly_chart(fig, use_container_width=True, config={"displaylogo": False, "scrollZoom": True})
    st.caption(f"Dot colour = how full the bin is: {fmt.legend(fmt.NOW_WORDS)}. Ring colour = the truck whose "
               "area the bin belongs to.")
    vname = {k: v["name"] for k, v in vehicles.items()}
    only = st.segmented_control("Show", ["all", "half", "full"], default="all", key="bins_filter",
                                format_func={"all": "All bins", "half": "Over half full",
                                             "full": "Full now"}.get) or "all"
    rows = []
    for b in sorted(bins, key=lambda x: -fills.get(x["id"], 0)):
        f = fills.get(b["id"], 0)
        if (only == "half" and f < 50) or (only == "full" and f < 100):
            continue
        rows.append({"place": b["landmark"], "street": b["street"], "type": SITE_LABEL.get(b["site_type"], b["site_type"]),
                     "fill": (f, status[b["id"]]), "status": STATUS_PILL[status[b["id"]]], "basis": basis.get(b["id"], ""),
                     "full_at": full_at.get(b["id"], ""),
                     "every": every_text(b),
                     "area": vname.get(bmap.get(b["id"], ""), "-"), "map": (b["lat"], b["lon"])})
    table(rows, [Col("place", "Place"), Col("street", "Street"), Col("type", "Type"),
                 Col("fill", "How full now (estimate)", "bar"), Col("status", "Status", "pill"),
                 Col("full_at", "Full at"),
                 Col("basis", "Based on"), Col("every", "Collected"), Col("area", "Truck area"),
                 Col("map", "", "map")], max_height=620)
    if can("edit_bins"):
        with st.expander("Enter a reading (supervisor visit)"):
            with st.form("manual_reading", clear_on_submit=True):
                bid = st.selectbox("Bin", [b["id"] for b in bins], index=None, placeholder="Choose the bin",
                                   format_func=lambda x: bin_label(next(b for b in bins if b["id"] == x)))
                f = st.select_slider("How full is it?", [0, 10, 25, 50, 75, 90, 100], value=50)
                if st.form_submit_button("Save reading") and bid:
                    db.add_reading(c, bid, f, "supervisor", user=user()["username"])
                    log("reading_entered", {"bin_id": bid, "fill": f})
                    mark_seen()
                    st.success(f"Saved: {next(b['landmark'] for b in bins if b['id'] == bid)} is {f}% full.")


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
                from wastewise import llm
                if llm.status(c)["ok"] and box.button("Ask Granite which place is meant"):
                    words = llm.complaint_place_words(c, text)
                    if words:
                        und2 = complaints.understand(" ".join(words) + " " + text, _index())
                        st.session_state.understood = (text, und2)
                        st.session_state.granite_words = words
                        st.rerun()
                    box.info("Granite did not find a place name either.")
            if st.session_state.get("granite_words") and und.decision != "none":
                box.caption("Place words suggested by IBM Granite: " + ", ".join(st.session_state.granite_words)
                            + " - please confirm the bin.")
            choice = box.radio(q, options, index=None, key="complaint_choice",
                               format_func=lambda x: bin_label(by_id[x]))
            other = box.selectbox("Or pick another bin", [b["id"] for b in bins], index=None,
                                  placeholder="(only if none above is right)", key="complaint_other",
                                  format_func=lambda x: bin_label(by_id[x]))
            chosen = other or choice
            if box.button("Confirm and save", type="primary", disabled=chosen is None):
                complaints.record(c, text, chosen, und, user()["username"], reporter or None)
                st.session_state.pop("understood", None)
                st.session_state.highlight_bin = chosen
                mark_seen()
                st.success(f"Saved for {by_id[chosen]['landmark']}. The bin now shows as full on every screen "
                           "and the control room sees it under Live alerts.")
    st.subheader("Complaint register")
    flt = st.segmented_control("Show", ["new", "open", "resolved", "all"], default="open", key="compl_filter",
                               format_func={"new": "new (place to confirm)", "open": "open", "resolved": "resolved",
                                            "all": "all"}.get) or "open"
    q = "SELECT * FROM complaints" + ("" if flt == "all" else " WHERE status = ?") + " ORDER BY ts DESC LIMIT 200"
    rows = db.rows(c, q, () if flt == "all" else (flt,))
    if not rows:
        st.info("No complaints.")
        return
    lang = {"en": "English", "hi": "Hindi", "mr": "Marathi", "mixed": "mixed"}
    status_pill = {"new": ("Place to confirm", "orange"), "open": ("Open", "red"), "resolved": ("Resolved", "green")}

    def place(r):
        if r["bin_id"] not in by_id:
            return "place not confirmed yet"
        p = by_id[r["bin_id"]]["landmark"]
        if r["original_bin_id"] and r["original_bin_id"] in by_id:
            p += f" (first filed at {by_id[r['original_bin_id']]['landmark']})"
        return p

    table([{"when": when(r["ts"]), "place": place(r),
            "problem": complaints.PROBLEM_WORDS.get(r["problem"], r["problem"] or "other"),
            "text": r["text"], "lang": lang.get(r["lang"], r["lang"] or ""), "status": status_pill.get(r["status"]),
            "demo": "demo data" if r["synthetic"] else ""} for r in rows],
          [Col("when", "When"), Col("place", "Place"), Col("problem", "Problem"), Col("text", "What was said"),
           Col("lang", "Language"), Col("status", "Status", "pill"), Col("demo", "")], max_height=480)

    def label(cid):
        r = next(x for x in rows if x["id"] == cid)
        return f"{when(r['ts'])} · {place(r)} · {complaints.PROBLEM_WORDS.get(r['problem'], r['problem'] or 'other')}"

    if can("complaints"):
        open_ids = [r["id"] for r in rows if r["status"] == "open"]
        col1, col2 = st.columns(2)
        with col1.form("resolve"):
            cid = st.selectbox("Mark as resolved", open_ids, index=None, placeholder="choose the complaint",
                               format_func=label)
            if st.form_submit_button("Resolve") and cid:
                complaints.resolve(c, cid, user()["username"])
                mark_seen()
                st.rerun()
        with col2.form("move"):
            cid = st.selectbox("Move a report to the right bin", [r["id"] for r in rows], index=None,
                               placeholder="choose the complaint", format_func=label)
            nb = st.selectbox("Right bin", [b["id"] for b in bins], index=None, placeholder="choose the bin",
                              format_func=lambda x: bin_label(by_id[x]))
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
