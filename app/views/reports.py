"""Reports: bin sizing, patterns, festival effect, 7-day outlook, savings vs routine, forecast accuracy."""
from __future__ import annotations

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from wastewise import calendar_in, db, reports

from ui.common import conn, synthetic_notice

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@st.cache_data(ttl=300, show_spinner="Calculating ...")
def _data(version: int, now: int) -> dict:
    c = db.connect()
    hist = reports.daily_volume(c, now)
    return {"sizing": reports.bin_sizing(c, now), "patterns": {k: v.tolist() for k, v in reports.patterns(c, now).items()},
            "hist": hist, "fest": reports.festival_effect(hist), "outlook": reports.outlook(c, now),
            "savings": reports.savings(c), "accuracy": reports.forecast_accuracy(c)}


def page() -> None:
    st.title("Reports")
    synthetic_notice()
    c = conn()
    now = db.now() // 600 * 600
    d = _data(db.data_version(c), now)
    t = st.tabs(["Bin sizing", "Weekly & hourly patterns", "Festival effect", "7-day outlook",
                 "Savings vs routine", "Forecast accuracy"])

    with t[0]:
        df = pd.DataFrame(d["sizing"])
        need = df[df.recommendation.str.startswith("too small")]
        over = df[df.recommendation.str.startswith("over-serviced")]
        a, b = st.columns(2)
        a.metric("Bins too small (bigger bin / extra pickup)", len(need))
        b.metric("Over-serviced bins", len(over))
        st.caption("Based on how full crews found each bin at collection over the last 28 days.")
        st.dataframe(df.rename(columns={"bin_id": "Bin", "landmark": "Place", "site_type": "Type",
                                        "capacity_l": "Capacity (L)", "every_days": "Every (days)",
                                        "collections": "Collections", "mean_fill_at_collection": "Avg fill at pickup %",
                                        "share_full_at_collection": "Share full at pickup",
                                        "full_readings": "Full readings", "recommendation": "Recommendation"})
                     .sort_values("Avg fill at pickup %", ascending=False),
                     hide_index=True, use_container_width=True)

    with t[1]:
        types = sorted(d["patterns"])
        st_ = st.selectbox("Site type", types, index=types.index("housing") if "housing" in types else 0)
        z = d["patterns"][st_]
        fig = go.Figure(go.Heatmap(z=z, x=[f"{h:02d}" for h in range(24)], y=DAYS, colorscale="Blues",
                                   colorbar=dict(title="% / hour")))
        fig.update_layout(height=320, margin=dict(l=0, r=0, t=10, b=0), xaxis_title="hour of day")
        st.plotly_chart(fig, use_container_width=True)
        tot = pd.DataFrame({k: [sum(row) for row in v] for k, v in d["patterns"].items()}, index=DAYS)
        st.caption("Filling per day (% of capacity) by site type")
        st.dataframe(tot.round(0), use_container_width=True)

    with t[2]:
        h = pd.DataFrame(d["hist"])
        if h.empty:
            st.info("No collections recorded yet.")
        else:
            fig = px.bar(h, x="date", y="litres", color=h["festival"].fillna("normal day"),
                         labels={"litres": "litres collected", "color": ""})
            fig.update_layout(height=360, margin=dict(l=0, r=0, t=10, b=0), legend_title_text="")
            st.plotly_chart(fig, use_container_width=True)
            if d["fest"]:
                st.dataframe(pd.DataFrame(d["fest"]).rename(columns={"festival": "Festival", "days": "Days",
                                                                     "mean_litres": "Litres per day",
                                                                     "vs_normal": "x normal day"}),
                             hide_index=True)
            st.caption("Festival dates are approximate (lunar calendar) - check against the official holiday list.")

    with t[3]:
        o = pd.DataFrame(d["outlook"])
        fig = go.Figure()
        fig.add_bar(x=o["date"], y=o["expected_litres"], name="expected waste (L)",
                    marker_color=["#DC2626" if x else "#2563EB" for x in o["over_capacity"]],
                    text=o["festival"].fillna(""), textposition="outside")
        fig.add_scatter(x=o["date"], y=o["fleet_capacity_litres"], name="fleet capacity per shift", mode="lines",
                        line=dict(color="#0F172A", dash="dash"))
        fig.update_layout(height=360, margin=dict(l=0, r=0, t=10, b=0))
        st.plotly_chart(fig, use_container_width=True)
        bad = o[o.over_capacity]
        if len(bad):
            st.error("Expected waste exceeds what the fleet can carry on: " + ", ".join(bad["date"])
                     + ". Plan an extra truck or a second round.")
        else:
            st.success("The fleet can carry the expected waste on each of the next 7 days.")
        st.caption("Expected waste = each bin's learned weekly pattern, raised on festival days by the festival "
                   "effect measured in the history. Fleet capacity = truck capacities x landfill trips per shift "
                   "in the latest approved plan.")

    with t[4]:
        s = d["savings"]
        if not s["plans"]:
            st.info("Savings appear once daily plans have been approved.")
        else:
            st.write(f"Across **{s['plans']}** approved daily plan(s), compared with the nearest-bin routine on the "
                     "same day and data" + (" - **demonstration data**." if s["demonstration"] else "."))
            names = {"distance_km": "Distance (km)", "fuel_l": "Fuel (L)", "co2_kg": "CO₂ (kg)",
                     "expected_overflow_min": "Expected overflow (bin-min)", "bins_full_on_arrival": "Bins full on arrival"}
            st.dataframe(pd.DataFrame([{"": names[k], "WasteWise": v["wastewise"], "Routine": v["routine"],
                                        "Saved": v["saved"]} for k, v in s["totals"].items()]),
                         hide_index=True, use_container_width=True)

    with t[5]:
        acc = d["accuracy"]
        if not acc["promoted"]:
            st.info("No forecaster trained yet (Settings > AI).")
        else:
            m = acc["promoted"]["metrics"]
            st.write(f"Promoted model #{acc['promoted']['id']} ({m['best_model']}, {m.get('gbt_library')}). "
                     f"Locked test window {m['test_window']['test_start'][:10]} to {m['test_window']['test_end'][:10]} "
                     f"- {m['label']}.")
            st.dataframe(pd.DataFrame([{"Model / baseline": k, "Error (fill points per 3 h)": v["mae_3h_points"],
                                        "Bias": v["bias_3h_points"]} for k, v in m["test"].items()])
                         .sort_values("Error (fill points per 3 h)"), hide_index=True)
        if acc["mildura"]:
            mm = acc["mildura"]
            st.subheader("Real-world test: Mildura smart bins")
            st.caption(mm["label"])
            st.dataframe(pd.DataFrame([{"Model / baseline": k, "Error (fill points per 3 h)": v["mae_3h_points"]}
                                       for k, v in mm["test"].items()]).sort_values("Error (fill points per 3 h)"),
                         hide_index=True)
            if mm.get("transfer_from_sanpada_model"):
                st.caption("The model trained on synthetic Sanpada data does not transfer to Mildura: " + ", ".join(
                    f"{k} {v['mae_3h_points']}" for k, v in mm["transfer_from_sanpada_model"].items()))
