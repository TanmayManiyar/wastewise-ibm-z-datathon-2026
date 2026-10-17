"""Reports in plain words: bin sizing, when bins fill, festivals, next 7 days, savings, forecast accuracy."""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import plotly.graph_objects as go
import streamlit as st

from wastewise import area, db, fmt, forecast, reports

from ui.common import can, conn, synthetic_notice
from ui.tables import Col, table

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
SITE_LABEL = {s.key: s.label for s in area.SITE_TYPES}
SIZES = [120, 240, 360, 660, 1100, 2400, 4500]
METHOD = {"gbt": "AI model (LightGBM boosted trees)", "hgb": "AI model (scikit-learn HistGradientBoosting)",
          "ensemble": "Both AI models averaged", "same_hour_last_7_days": "Simple rule: same hour on recent days",
          "mean_rate_last_7_days": "Simple rule: last week's average speed",
          "last_24_hours": "Simple rule: speed of the last 24 hours"}


def _day_label(iso: str) -> str:
    d = datetime.strptime(iso, "%Y-%m-%d")
    return f"{d:%a} {d.day} {d:%b}"


def _hour(h: int) -> str:
    return f"{h % 12 or 12} {'am' if h < 12 else 'pm'}"


@st.cache_data(ttl=300, show_spinner="Calculating ...")
def _data(version: int, now: int) -> dict:
    c = db.connect()
    hist = reports.daily_volume(c, now)
    return {"sizing": reports.bin_sizing(c, now), "patterns": {k: v.tolist() for k, v in reports.patterns(c, now).items()},
            "hist": hist, "fest": reports.festival_effect(hist), "outlook": reports.outlook(c, now),
            "savings": reports.savings(c), "accuracy": reports.forecast_accuracy(c)}


@st.cache_data(ttl=600, show_spinner="Testing the forecast on the held-back days ...")
def _retest(version: int) -> dict | None:
    return forecast.evaluate_promoted(db.connect())


@st.cache_data(ttl=600, show_spinner="Checking how well WasteWise knew each bin's fill ...")
def _state_risk(version: int) -> dict | None:
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root / "scripts"))
    try:
        from eval_state_risk import evaluate
        return evaluate(db.connect(), days=7)
    except Exception:
        return None


def sizing_tab(d: dict) -> None:
    rows = [r for r in d["sizing"] if r["collections"] >= 5]
    small = [r for r in rows if r["recommendation"].startswith("too small")]
    over = [r for r in rows if r["recommendation"].startswith("over-serviced")]
    ok = len(rows) - len(small) - len(over)
    a, b, c = st.columns(3)
    a.metric("Bins that are too small", len(small))
    b.metric("Bins collected more often than needed", len(over))
    c.metric("Bins that are the right size", ok)
    st.caption("Based on how full the crews found each bin when they emptied it, over the last 4 weeks.")
    if small:
        st.markdown("##### Too small - they are often already full when the truck comes")

        def bigger(cap):
            return next((s for s in SIZES if s > cap), cap * 2)
        table([{"place": r["landmark"], "type": SITE_LABEL.get(r["site_type"], r["site_type"]),
                "size": fmt.litres(r["capacity_l"]),
                "full": f"{round(r['share_full_at_collection'] * r['collections'])} of {r['collections']} pickups",
                "fill": r["mean_fill_at_collection"],
                "todo": f"Replace with a {fmt.litres(bigger(r['capacity_l']))} bin, or add a second pickup"}
               for r in sorted(small, key=lambda r: -r["share_full_at_collection"])],
              [Col("place", "Place"), Col("type", "Type"), Col("size", "Bin now"), Col("full", "Found full at"),
               Col("fill", "Usually this full at pickup", "bar"), Col("todo", "What to do")])
    if over:
        st.markdown("##### Collected more often than needed")
        table([{"place": r["landmark"], "type": SITE_LABEL.get(r["site_type"], r["site_type"]),
                "fill": r["mean_fill_at_collection"],
                "todo": "Collect every 2 days" if r["every_days"] == 1 else "A smaller bin would do"} for r in over],
              [Col("place", "Place"), Col("type", "Type"), Col("fill", "Usually this full at pickup", "bar"),
               Col("todo", "What to do")])
    elif rows:
        st.success("No bin is collected more often than it needs.")


def patterns_tab(d: dict) -> None:
    types = sorted(d["patterns"], key=lambda k: SITE_LABEL.get(k, k))
    default = types.index("housing") if "housing" in types else 0
    t = st.selectbox("Type of place", types, index=default, format_func=lambda k: SITE_LABEL.get(k, k))
    z = d["patterns"][t]
    per_day = [sum(row) for row in z]
    by_hour = [sum(z[dw][h] for dw in range(7)) / 7 for h in range(24)]
    peak = max(range(24), key=lambda h: by_hour[h])
    busiest, quietest = max(range(7), key=lambda i: per_day[i]), min(range(7), key=lambda i: per_day[i])
    st.markdown(f"**{SITE_LABEL.get(t, t)}:** a bin fills about **{sum(per_day) / 7:.0f}% of its size per day**. "
                f"Busiest around **{_hour(peak)}**. Busiest day **{DAYS[busiest]}** ({per_day[busiest]:.0f}%), "
                f"quietest **{DAYS[quietest]}** ({per_day[quietest]:.0f}%).")
    fig = go.Figure(go.Heatmap(z=z, x=[_hour(h) for h in range(24)], y=DAYS, colorscale="Blues",
                               colorbar=dict(title="% filled<br>per hour"),
                               hovertemplate="%{y} %{x}: %{z:.1f}% of the bin per hour<extra></extra>"))
    fig.update_layout(height=320, margin=dict(l=0, r=0, t=10, b=0), yaxis=dict(autorange="reversed"))
    st.plotly_chart(fig, use_container_width=True, config={"displaylogo": False})
    st.caption("Darker = the bin fills faster at that hour.")
    rows = []
    for k in types:
        pdays = [sum(row) for row in d["patterns"][k]]
        rows.append({"type": SITE_LABEL.get(k, k), "per_day": f"{sum(pdays) / 7:.0f}% of the bin",
                     "busy": DAYS[max(range(7), key=lambda i: pdays[i])],
                     "quiet": DAYS[min(range(7), key=lambda i: pdays[i])]})
    st.markdown("##### All types of place")
    table(rows, [Col("type", "Type of place"), Col("per_day", "Fills per day"), Col("busy", "Busiest day"),
                 Col("quiet", "Quietest day")])


def festival_tab(d: dict) -> None:
    h = d["hist"]
    if not h:
        st.info("No collections recorded yet.")
        return
    for f in d["fest"]:
        pct = (f["vs_normal"] - 1) * 100 if f["vs_normal"] else 0
        st.markdown(f"- **{f['festival']}** ({f['days']} days): **{abs(pct):.0f}% {'more' if pct >= 0 else 'less'}** "
                    f"waste per day than a normal day ({fmt.litres(f['mean_litres'])} a day).")
    fig = go.Figure()
    normal = [r for r in h if not r["festival"]]
    fig.add_bar(x=[r["date"] for r in normal], y=[r["litres"] for r in normal], name="normal day",
                marker_color="#94A3B8")
    for name in sorted({r["festival"] for r in h if r["festival"]}):
        sel = [r for r in h if r["festival"] == name]
        fig.add_bar(x=[r["date"] for r in sel], y=[r["litres"] for r in sel], name=name)
    fig.update_layout(height=340, margin=dict(l=0, r=0, t=10, b=0), barmode="overlay", yaxis_title="litres collected",
                      legend=dict(orientation="h"))
    st.plotly_chart(fig, use_container_width=True, config={"displaylogo": False})
    st.caption("Litres collected each day. Festival dates are approximate - check against the official holiday list.")


def outlook_tab(d: dict) -> None:
    o = d["outlook"]
    bad = [x for x in o if x["over_capacity"]]
    if bad:
        st.error("The trucks cannot carry everything on: " + ", ".join(_day_label(x["date"]) for x in bad)
                 + ". Plan an extra truck or a second round on those days.")
    else:
        st.success("The trucks can carry the expected waste on each of the next 7 days.")
    table([{"day": _day_label(x["date"]), "fest": x["festival"] or "", "waste": fmt.litres(x["expected_litres"]),
            "cap": fmt.litres(x["fleet_capacity_litres"]),
            "used": round(100 * x["expected_litres"] / x["fleet_capacity_litres"]),
            "ok": ("Extra trip needed", "red") if x["over_capacity"] else ("OK", "green")} for x in o],
          [Col("day", "Day"), Col("fest", "Festival"), Col("waste", "Expected waste"),
           Col("cap", "Trucks can carry"), Col("used", "How full the trucks will be", "bar"), Col("ok", "", "pill")])
    st.caption("Expected waste comes from each bin's usual weekly pattern, raised on festival days by the festival "
               "effect seen in the history. 'Trucks can carry' = all trucks x their usual landfill trips per shift.")


def savings_tab(d: dict) -> None:
    s = d["savings"]
    if not s["plans"]:
        st.info("Savings appear once daily plans have been approved.")
        return
    t = s["totals"]
    st.markdown(f"Compared with the old routine (each truck drives to the nearest waiting bin), over "
                f"**{s['plans']} approved day(s)**" + (" - **demonstration data**:" if s["demonstration"] else ":"))
    a, b, c, e = st.columns(4)
    a.metric("Less driving", fmt.km(t["distance_km"]["saved"]),
             f"{fmt.km(t['distance_km']['wastewise'])} instead of {fmt.km(t['distance_km']['routine'])}", delta_color="off")
    b.metric("Less diesel", f"{t['fuel_l']['saved']:.0f} L", delta_color="off")
    c.metric("Less CO₂", f"{t['co2_kg']['saved']:.0f} kg", delta_color="off")
    e.metric("Fewer hours of overflowing bins", f"{t['expected_overflow_min']['saved'] / 60:.1f} h",
             f"{t['expected_overflow_min']['wastewise'] / 60:.1f} h instead of "
             f"{t['expected_overflow_min']['routine'] / 60:.1f} h", delta_color="off")


def accuracy_tab(d: dict) -> None:
    v = db.data_version(conn())
    r = _retest(v)
    acc = d["accuracy"]
    if not r:
        st.info("No forecast model trained yet (Settings > AI).")
    else:
        st.markdown("##### How well does WasteWise predict how fast bins fill?")
        st.markdown(
            f"We hid the days **{_day_label(r['test_from'])} to {_day_label(r['test_to'])}** from the model while "
            f"training, then asked it to predict them. Over the next 3 hours its prediction is off by "
            f"**{r['model_mae']:.1f}% of the bin** on average. The best simple rule is off by "
            f"**{r['baseline_mae']:.1f}%**, so the AI is **{r['improvement_pct']:.0f}% more accurate**. "
            f"**{r['share_within_5_points'] * 100:.0f}%** of its predictions are within 5% of the truth.")
        a, b, c = st.columns(3)
        a.metric("AI forecast error", f"{r['model_mae']:.1f}% of the bin", "per 3 hours", delta_color="off")
        b.metric("Best simple rule", f"{r['baseline_mae']:.1f}% of the bin", METHOD.get(r["baseline"], ""),
                 delta_color="off")
        c.metric("AI is better by", f"{r['improvement_pct']:.0f}%", delta_color="off")
        if acc["promoted"]:
            m = acc["promoted"]["metrics"]
            rows = [{"method": METHOD.get(k, k), "err": x["mae_3h_points"],
                     "used": ("used", "green") if k == m["best_model"] else None}
                    for k, x in sorted(m["test"].items(), key=lambda kv: kv[1]["mae_3h_points"])]
            table([dict(x, err_txt=f"{x['err']:.1f}% of the bin") for x in rows],
                  [Col("method", "Method"), Col("err_txt", "Average error over 3 hours"), Col("used", "", "pill")])
        fig = go.Figure()
        fig.add_scatter(x=[x["hours_ahead"] for x in r["by_lead"]], y=[x["model"] for x in r["by_lead"]],
                        name="AI model", mode="lines+markers", line=dict(color="#2563EB"))
        fig.add_scatter(x=[x["hours_ahead"] for x in r["by_lead"]], y=[x["baseline"] for x in r["by_lead"]],
                        name="best simple rule", mode="lines+markers", line=dict(color="#94A3B8", dash="dash"))
        fig.update_layout(height=280, margin=dict(l=0, r=0, t=30, b=0), xaxis_title="hours ahead",
                          yaxis_title="average error (% of bin)", title="Error by how far ahead we predict",
                          legend=dict(orientation="h"))
        st.plotly_chart(fig, use_container_width=True, config={"displaylogo": False})
        with st.expander("Accuracy by type of place"):
            table([{"type": SITE_LABEL.get(x["site_type"], x["site_type"]), "ai": f"{x['model']:.1f}%",
                    "rule": f"{x['baseline']:.1f}%",
                    "win": ("AI better", "green") if x["model"] < x["baseline"] else ("rule better", "amber")}
                   for x in r["by_site_type"]],
                  [Col("type", "Type of place"), Col("ai", "AI error"), Col("rule", "Simple rule error"),
                   Col("win", "", "pill")])
        st.caption(f"Tested on {r['rows']:,} predictions - {r['label']}.")
        if can("settings") and st.button("Re-test the model now"):
            _retest.clear()
            _state_risk.clear()
            st.rerun()

    sr = _state_risk(db.data_version(conn()))
    if sr:
        st.markdown("##### How well does WasteWise know each bin's fill right now?")
        st.markdown(f"- Bins **with a sensor**: off by **{sr['state_mae_sensor']:.0f}% of the bin** on average.\n"
                    f"- Bins **without a sensor** (crew and supervisor estimates only): off by "
                    f"**{sr['state_mae_manual']:.0f}%** - adding sensors to these bins would help most.\n"
                    f"- **Overflow warnings:** when WasteWise says a bin is very likely full (over 90%), it is full "
                    + next((f"**{x['observed'] * 100:.0f}%** of the time" for x in sr["calibration"]
                            if x["bin"].startswith("0.9")), "most of the time") + ".")
        st.caption(f"Checked against the true fill of the last 7 days - {sr['label']}.")

    if acc["mildura"]:
        mm = acc["mildura"]
        t = mm["test"]
        best_rule = min((k for k in t if k in ("same_hour_last_7_days", "mean_rate_last_7_days", "last_24_hours")),
                        key=lambda k: t[k]["mae_3h_points"])
        ai = t["gbt"]["mae_3h_points"]
        rule = t[best_rule]["mae_3h_points"]
        st.markdown("##### Real-world test: smart bins in Mildura, Australia")
        st.markdown(f"The same method trained and tested on **real sensor data** from {len(mm['devices'])} bins of "
                    f"Mildura Rural City Council: the AI is off by **{ai:.1f}%** of the bin per 3 hours, the best "
                    f"simple rule by **{rule:.1f}%** - **{(1 - ai / rule) * 100:.0f}% more accurate** on real data.")
        if mm.get("transfer_from_sanpada_model"):
            tr = mm["transfer_from_sanpada_model"].get("sanpada_gbt", {}).get("mae_3h_points")
            if tr:
                st.caption(f"Honest note: the model trained on our synthetic Sanpada data does *not* work as well in "
                           f"Mildura ({tr:.1f}%) - a model must be trained on each city's own data.")
        st.caption(mm["label"])


def page() -> None:
    st.title("Reports")
    synthetic_notice()
    c = conn()
    now = db.now() // 600 * 600
    d = _data(db.data_version(c), now)
    t = st.tabs(["Bin sizes", "When bins fill", "Festivals", "Next 7 days", "Savings", "Forecast accuracy"])
    with t[0]:
        sizing_tab(d)
    with t[1]:
        patterns_tab(d)
    with t[2]:
        festival_tab(d)
    with t[3]:
        outlook_tab(d)
    with t[4]:
        savings_tab(d)
    with t[5]:
        accuracy_tab(d)
