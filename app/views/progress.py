"""Progress: planned vs actual per truck, exceptions with what to do, paper sheets, CSV."""
from __future__ import annotations

import streamlit as st

from wastewise import db, pdfs, progress

from ui.common import can, conn, hhmm, log, mark_seen, user
from ui.tables import Col, table

STATUS_LABEL = {"collected": "Collected", "late": "Collected late", "could_not_reach": "Could not reach",
                "bin_missing": "Bin missing", "overdue": "Overdue", "missed": "Missed", "pending": "To do"}
STATUS_COLOUR = {"collected": "green", "late": "amber", "could_not_reach": "red", "bin_missing": "red",
                 "overdue": "orange", "missed": "red", "pending": "grey"}


def tab(day) -> None:
    c = conn()
    d = day.isoformat()
    rows = progress.status_rows(c, d, db.now())
    if not rows:
        st.info("Progress appears here once a plan for the day is approved.")
        return
    summ = progress.summary(rows)
    cols = st.columns(len(summ))
    for col, (veh, s) in zip(cols, summ.items()):
        with col.container(border=True):
            st.markdown(f"**{veh}**")
            st.progress(s["collected"] / max(s["planned"], 1), text=f"{s['collected']} of {s['planned']} collected")
            bits = [f"{s['late']} late" if s["late"] else "", f"{s['problems']} problem(s)" if s["problems"] else "",
                    f"{s['overdue']} overdue" if s["overdue"] else "", f"{s['missed']} missed" if s["missed"] else ""]
            st.caption(", ".join(b for b in bits if b) or "No problems so far")
    ex = [r for r in rows if r["status"] in ("late", "could_not_reach", "bin_missing", "overdue", "missed")]
    if ex:
        st.subheader(f"Exceptions ({len(ex)})")
        for r in ex:
            fn = st.error if r["status"] in ("could_not_reach", "bin_missing", "missed") else st.warning
            fn(f"**{r['landmark']}** ({r['vehicle_name']}, planned {hhmm(r['planned_ts'])}): "
               f"{STATUS_LABEL[r['status']]}" + (f", {r['delay_min']} min after the planned time" if r["delay_min"] else "")
               + f"  \nWhat to do: {r['action']}")
    def late_text(r):
        if r["delay_min"] is None:
            return ""
        return "on time" if abs(r["delay_min"]) <= 5 else (f"{r['delay_min']} min late" if r["delay_min"] > 0
                                                           else f"{-r['delay_min']} min early")
    evening_too = any(r.get("round") == "Evening" for r in rows)
    table([{"round": r.get("round", ""), "truck": r["vehicle_name"], "place": r["landmark"], "planned": hhmm(r["planned_ts"]),
            "actual": hhmm(r["actual_ts"]) if r["actual_ts"] else "", "late": late_text(r),
            "status": (STATUS_LABEL[r["status"]], STATUS_COLOUR[r["status"]]),
            "fill": f"{r['fill_reported']:.0f}%" if r["fill_reported"] is not None else "", "by": r["by"] or ""}
           for r in rows],
          ([Col("round", "Round")] if evening_too else []) +
          [Col("truck", "Truck"), Col("place", "Place"), Col("planned", "Planned"), Col("actual", "Done at"),
           Col("late", ""), Col("status", "Status", "pill"), Col("fill", "How full (crew said)"),
           Col("by", "Recorded by")], max_height=520)
    a, b, _ = st.columns([1, 1, 2])
    pdf = pdfs.shift_report(d, rows, summ, STATUS_LABEL, demo=bool(db.get_meta(c, "synthetic_info")))
    if a.download_button("End-of-shift report (PDF)", pdf, file_name=f"end_of_shift_{d}.pdf",
                         mime="application/pdf", icon=":material/picture_as_pdf:"):
        log("progress_pdf_downloaded", {"date": d})
    if b.download_button("Same as spreadsheet (CSV)", progress.to_csv(rows, hhmm), file_name=f"end_of_shift_{d}.csv",
                         mime="text/csv", icon=":material/download:"):
        log("progress_csv_downloaded", {"date": d})

    if can("record_progress"):
        with st.expander("Enter a returned paper route sheet"):
            todo = [r for r in rows if r["status"] in ("pending", "overdue", "missed")]
            plans = {p["id"]: p for p in progress.day_plans(c, d)}
            with st.form("paper", clear_on_submit=True):
                bid = st.selectbox("Bin", [r["bin_id"] for r in todo], index=None, placeholder="Bin on the sheet",
                                   format_func=lambda x: next(f"{r['landmark']} ({r['vehicle_name']}, planned {hhmm(r['planned_ts'])})"
                                                              for r in todo if r["bin_id"] == x))
                outcome = st.radio("What happened", ["collected", "could_not_reach", "bin_missing"],
                                   format_func=STATUS_LABEL.get, horizontal=True)
                fill = st.select_slider("How full was it", [25, 50, 75, 100, 120], value=75,
                                        format_func=lambda v: "overflowing" if v == 120 else f"{v}%")
                tm = st.time_input("Time done (from the sheet)", value=None)
                if st.form_submit_button("Save from paper sheet") and bid:
                    r = next(x for x in todo if x["bin_id"] == bid)
                    ts = None
                    if tm is not None:
                        from datetime import datetime
                        from wastewise import config
                        ts = int(datetime.combine(day, tm, tzinfo=config.IST).timestamp())
                    progress.record_crew(c, plans[r["plan_id"]], r["vehicle_id"], outcome, user()["username"], bid,
                                         float(fill), ts=ts, note="entered from paper route sheet")
                    mark_seen()
                    st.success(f"Saved {bid}: {STATUS_LABEL[outcome]}.")
