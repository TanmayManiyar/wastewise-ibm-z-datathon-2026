"""Crew phone page "My route": next stop in large text, big buttons, three languages."""
from __future__ import annotations

import urllib.parse

import streamlit as st

from wastewise import db, planner, progress

from ui.common import conn, hhmm, live_refresh, mark_seen, user
from ui.i18n import LANGS, t

FILL_CHOICES = [("q", 25.0), ("h", 50.0), ("t", 75.0), ("f", 100.0), ("o", 120.0)]


def _maps_url(lat: float, lon: float) -> str:
    q = urllib.parse.urlencode({"api": 1, "destination": f"{lat:.6f},{lon:.6f}", "travelmode": "driving"})
    return f"https://www.google.com/maps/dir/?{q}"


def _language(key: str) -> str:
    """Language picker with one source of truth. Tapping the selected language again must not
    switch the page back to English (a segmented control returns None when deselected)."""
    sk, last = f"{key}_lang_sel", f"{key}_lang"
    st.session_state.setdefault(last, "en")
    if st.session_state.get(sk) is None:
        st.session_state[sk] = st.session_state[last]
    lang = st.segmented_control("Language / भाषा", list(LANGS), format_func=LANGS.get, key=sk) \
        or st.session_state[last]
    st.session_state[last] = lang
    return lang


def render(vehicle_id: str, day: str, interactive: bool = True, key: str = "crew", title: str | None = None) -> None:
    c = conn()
    lang = _language(key)
    if title:
        st.markdown(f"## {t('title', lang)} · {title.replace('Truck', t('truck', lang)).replace('(small)', t('small', lang))}")
    plan, route = progress.route_for(c, day, vehicle_id)
    if not route:
        st.info(t("no_route", lang))
        return
    seen = st.session_state.get(f"{key}_plan_seen")
    if seen and seen != plan["id"]:
        st.warning(t("updated", lang) + f" ({hhmm(plan.get('approved_at'))})", icon=":material/sync:")
    st.session_state[f"{key}_plan_seen"] = plan["id"]

    states = progress.stop_states(c, plan, route)
    bins = {b["id"]: b for b in db.bins(c, active_only=False)}
    vrow = next((v for v in db.vehicles(c, active_only=False) if v["id"] == vehicle_id), None)
    cap = (vrow["capacity_l"] if vrow else route["capacity_l"]) * 0.95
    load = progress.load_on_board(c, plan, route, bins)
    nbins = sum(1 for s in route["stops"] if s["kind"] == "bin")
    done_bins = sum(1 for s, st_ in zip(route["stops"], states) if s["kind"] == "bin" and st_ == "done")
    nxt = next((i for i, (s, st_) in enumerate(zip(route["stops"], states)) if st_ == "todo" and s["kind"] != "depot"),
               None)
    st.progress(done_bins / max(nbins, 1), text=f"{done_bins} / {nbins} · {t('load', lang)} ≈ {load:,.0f} L "
                                                  f"/ {cap:,.0f} L")
    if nxt is None:
        st.success(t("depot", lang), icon=":material/home:")
        return
    s = route["stops"][nxt]
    next_bin_l = 0.0
    if s["kind"] == "bin":
        b = bins[s["bin_id"]]
        next_bin_l = b["capacity_l"] * min(s["expected_fill_pct"], 120) / 100
    full_now = s["kind"] == "landfill" or (load > 0 and load + next_bin_l > cap)
    if full_now:
        st.error(f"### {t('truck_full', lang)}", icon=":material/local_shipping:")
        place = db.places(c)["landfill"]
        st.link_button(t("maps", lang), _maps_url(place["lat"], place["lon"]), use_container_width=True,
                       icon=":material/map:")
        if interactive and st.button(t("unloaded", lang), type="primary", use_container_width=True,
                                     key=f"{key}_unload"):
            progress.record_crew(c, plan, vehicle_id, "landfill_unload", user()["username"])
            mark_seen()
            st.rerun()
        if s["kind"] == "landfill":
            return
    n = sum(1 for x in route["stops"][:nxt + 1] if x["kind"] == "bin")
    st.markdown(f"<div class='ww-mid'>{t('next', lang)} · {n} {t('of', lang)} {nbins}</div>"
                f"<div class='ww-big'>{s['landmark']}</div><div>{s['bin_id']}</div>"
                f"<div class='ww-mid'>{s['street'] or ''}</div>"
                f"<div>{t('planned', lang)}: <b>{hhmm(s['arrive_ts'])}</b></div>", unsafe_allow_html=True)
    st.link_button(t("maps", lang), _maps_url(s["lat"], s["lon"]), use_container_width=True, icon=":material/map:")
    if not interactive:
        return
    fill = st.segmented_control(t("how_full", lang), [k for k, _ in FILL_CHOICES], format_func=lambda k: t(k, lang),
                                key=f"{key}_fill_{plan['id']}_{s['bin_id']}")
    fill_pct = dict(FILL_CHOICES).get(fill)
    c1, c2, c3 = st.columns(3)
    if c1.button(t("collected", lang), type="primary", use_container_width=True, key=f"{key}_ok_{s['bin_id']}",
                 icon=":material/check_circle:"):
        if fill_pct is None:
            st.warning(t("choose_fill", lang))
        else:
            progress.record_crew(c, plan, vehicle_id, "collected", user()["username"], s["bin_id"], fill_pct)
            mark_seen()
            st.rerun()
    if c2.button(t("not_reach", lang), use_container_width=True, key=f"{key}_nr_{s['bin_id']}", icon=":material/block:"):
        progress.record_crew(c, plan, vehicle_id, "could_not_reach", user()["username"], s["bin_id"], fill_pct)
        mark_seen()
        st.rerun()
    if c3.button(t("missing", lang), use_container_width=True, key=f"{key}_ms_{s['bin_id']}",
                 icon=":material/help:"):
        progress.record_crew(c, plan, vehicle_id, "bin_missing", user()["username"], s["bin_id"])
        mark_seen()
        st.rerun()
    with st.expander(t("remaining", lang)):
        k = 0
        for x, st_ in zip(route["stops"], states):
            if x["kind"] == "bin":
                k += 1
                mark = "✅" if st_ == "done" else "⬜"
                st.write(f"{mark} {k}. {hhmm(x['arrive_ts'])} · {x['landmark']}")
            elif x["kind"] == "landfill":
                st.write(f"{'✅' if st_ == 'done' else '🚛'} {hhmm(x['arrive_ts'])} · {t('landfill', lang)}")


def page() -> None:
    live_refresh(10)
    u = user()
    c = conn()
    day = planner.plan_day(c).isoformat()
    v = next((x for x in db.vehicles(c, active_only=False) if x["id"] == u["vehicle_id"]), None)
    render(u["vehicle_id"], day, interactive=True, key="crew", title=v["name"] if v else u["vehicle_id"])


def preview(day) -> None:
    """Office view of any truck's phone screen (read-only unless the user may record progress)."""
    c = conn()
    vs = db.vehicles(c)
    vid = st.radio("Truck", [v["id"] for v in vs], format_func=lambda x: next(v["name"] for v in vs if v["id"] == x),
                   horizontal=True, key="crew_preview_vehicle")
    st.caption("This is exactly what the crew sees on the phone. Office users can record progress on their behalf "
               "(for example when the crew phones in).")
    left, _ = st.columns([1, 1.4])
    with left.container(border=True):
        render(vid, day.isoformat(), interactive=True, key=f"pv_{vid}")
