"""Progress: planned vs actual per truck, exceptions with what to do, paper sheets, CSV."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from wastewise import db, progress

from ui.common import can, conn, hhmm, log, mark_seen, user

STATUS_LABEL = {"collected": "Collected", "late": "Late (> 30 min)", "could_not_reach": "Could not reach",
                "bin_missing": "Bin missing", "overdue": "Overdue", "missed": "Missed", "pending": "To do"}


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
            st.caption(f"late {s['late']} · problems {s['problems']} · overdue {s['overdue']} · missed {s['missed']}")
    ex = [r for r in rows if r["status"] in ("late", "could_not_reach", "bin_missing", "overdue", "missed")]
    if ex:
        st.subheader(f"Exceptions ({len(ex)})")
        for r in ex:
            fn = st.error if r["status"] in ("could_not_reach", "bin_missing", "missed") else st.warning
            fn(f"**{r['bin_id']} {r['landmark']}** ({r['vehicle_name']}, planned {hhmm(r['planned_ts'])}): "
               f"{STATUS_LABEL[r['status']]}" + (f", {r['delay_min']} min" if r["delay_min"] else "")
               + f"  \nWhat to do: {r['action']}")
    df = pd.DataFrame([{"Truck": r["vehicle_name"], "Bin": r["bin_id"], "Place": r["landmark"],
                        "Planned": hhmm(r["planned_ts"]), "Actual": hhmm(r["actual_ts"]) if r["actual_ts"] else "",
                        "Status": STATUS_LABEL[r["status"]], "Delay (min)": r["delay_min"],
                        "How full (crew)": f"{r['fill_reported']:.0f}%" if r["fill_reported"] is not None else "",
                        "By": r["by"] or ""} for r in rows])
    st.dataframe(df, hide_index=True, use_container_width=True)
    csv = progress.to_csv(rows, hhmm)
    if st.download_button("End-of-shift report (CSV)", csv, file_name=f"wastewise_progress_{d}.csv", mime="text/csv",
                          icon=":material/download:"):
        log("progress_csv_downloaded", {"date": d})

    if can("record_progress"):
        with st.expander("Enter a returned paper route sheet"):
            todo = [r for r in rows if r["status"] in ("pending", "overdue", "missed")]
            plans = {p["id"]: p for p in progress.day_plans(c, d)}
            with st.form("paper", clear_on_submit=True):
                bid = st.selectbox("Bin", [r["bin_id"] for r in todo], index=None, placeholder="Bin on the sheet",
                                   format_func=lambda x: next(f"{r['bin_id']} {r['landmark']} ({r['vehicle_name']})"
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
