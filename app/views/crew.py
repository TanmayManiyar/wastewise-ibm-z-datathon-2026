"""Crew phone page "My route": next stop in large text, big buttons, three languages."""
from __future__ import annotations

import urllib.parse

import streamlit as st

from wastewise import crews, db, fmt, planner, progress, proof

from ui.common import can, conn, hhmm, live_refresh, mark_seen, user
from ui import gps as gps_ui
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
    saved = st.session_state.pop(f"{key}_saved", None)
    if saved:
        st.success(f"✔ {t('saved', lang)}", icon=":material/check_circle:")
    plan, route = progress.route_for(c, day, vehicle_id)
    if not route:
        st.info(t("no_route", lang))
        return
    if plan["plan_date"] != planner.today().isoformat():
        st.info(f"{fmt.day_name(plan['plan_date'])} · {t('planned', lang)} {hhmm(plan['start_ts'])}",
                icon=":material/event:")
    if plan.get("kind") == "extra":
        st.info(f"🌆 {t('evening', lang)} · {hhmm(plan['start_ts'])}", icon=":material/nights_stay:")
    person, fix = None, None
    if interactive:
        person = _who(plan["plan_date"], vehicle_id, lang, key)
        if person is None:
            return          # the crew first says who is on the truck today
        if user()["role"] == "crew":
            _route_change(plan, vehicle_id, lang, key, person)
            fix = _location(lang, key)
    states = progress.stop_states(c, plan, route)
    bins = {b["id"]: b for b in db.bins(c, active_only=False)}
    vrow = next((v for v in db.vehicles(c, active_only=False) if v["id"] == vehicle_id), None)
    cap = (vrow["capacity_l"] if vrow else route["capacity_l"]) * 0.95
    load = progress.load_on_board(c, plan, route, bins)
    nbins = sum(1 for s in route["stops"] if s["kind"] == "bin")
    done_bins = sum(1 for s, st_ in zip(route["stops"], states) if s["kind"] == "bin" and st_ == "done")
    nxt = next((i for i, (s, st_) in enumerate(zip(route["stops"], states)) if st_ == "todo" and s["kind"] != "depot"),
               None)
    st.progress(done_bins / max(nbins, 1), text=f"{done_bins} / {nbins} · {t('load', lang)} ≈ {fmt.litres(load)} "
                                                  f"/ {fmt.litres(cap)}")
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
            progress.record_crew(c, plan, vehicle_id, "landfill_unload", user()["username"], person=person,
                                 gps=fix)
            st.session_state[f"{key}_saved"] = "landfill_unload"
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
    photo_file = st.file_uploader(t("photo", lang), type=["jpg", "jpeg", "png", "webp"],
                                  key=f"{key}_photo_{plan['id']}_{s['bin_id']}")

    def photo_path() -> str | None:
        if photo_file is None:
            return None
        try:
            return proof.save_photo(photo_file.getvalue(), plan["plan_date"])
        except ValueError as exc:
            st.error(str(exc))
            return None
    c1, c2, c3 = st.columns(3)
    if c1.button(t("collected", lang), type="primary", use_container_width=True, key=f"{key}_ok_{s['bin_id']}",
                 icon=":material/check_circle:"):
        if fill_pct is None:
            st.warning(t("choose_fill", lang))
        else:
            progress.record_crew(c, plan, vehicle_id, "collected", user()["username"], s["bin_id"], fill_pct,
                                 person=person, gps=fix, photo=photo_path())
            st.session_state[f"{key}_saved"] = "collected"
            mark_seen()
            st.rerun()
    if c2.button(t("not_reach", lang), use_container_width=True, key=f"{key}_nr_{s['bin_id']}", icon=":material/block:"):
        progress.record_crew(c, plan, vehicle_id, "could_not_reach", user()["username"], s["bin_id"], fill_pct,
                             person=person, gps=fix, photo=photo_path())
        st.session_state[f"{key}_saved"] = "could_not_reach"
        mark_seen()
        st.rerun()
    if c3.button(t("missing", lang), use_container_width=True, key=f"{key}_ms_{s['bin_id']}",
                 icon=":material/help:"):
        path = photo_path()
        if path is None and user()["role"] == "crew":
            st.warning(t("need_photo", lang))          # a missing bin needs a photo of the empty spot
        else:
            progress.record_crew(c, plan, vehicle_id, "bin_missing", user()["username"], s["bin_id"], person=person,
                                 gps=fix, photo=path)
            st.session_state[f"{key}_saved"] = "bin_missing"
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


def _location(lang: str, key: str) -> dict | None:
    """Phone position for proof of service, plus the 'Update my position' button."""
    fix, state = gps_ui.watch(f"{key}_gps")
    a, b = st.columns([2, 1])
    if state == "ok":
        a.caption(f"📍 {t('gps_ok', lang)} (±{int(fix['acc'])} m)")
    elif state in ("denied", "unavailable"):
        a.caption(f"📍 {t('gps_' + state, lang)}")
    else:
        a.caption(f"📍 {t('gps_wait', lang)}")
    if b.button(t("update_pos", lang), key=f"{key}_pos", disabled=fix is None, icon=":material/my_location:"):
        progress.record_position(conn(), user()["vehicle_id"], fix, user()["username"],
                                 person=st.session_state.get(f"{key}_person"))
        st.toast(t("pos_saved", lang))
        mark_seen()
    return fix


ALERT_JS = """<script>
(function() {
  const k = 'ww_route_alert_PLAN';
  try { if (sessionStorage.getItem(k)) return; sessionStorage.setItem(k, '1'); } catch (e) {}
  try { navigator.vibrate && navigator.vibrate([300, 150, 300, 150, 300]); } catch (e) {}
  try {
    const a = new (window.AudioContext || window.webkitAudioContext)();
    [0, 0.35].forEach(t => { const o = a.createOscillator(); o.frequency.value = 880;
      o.connect(a.destination); o.start(a.currentTime + t); o.stop(a.currentTime + t + 0.2); });
  } catch (e) {}
})();
</script>"""


def _route_change(plan: dict, vehicle_id: str, lang: str, key: str, person: str) -> None:
    """Banner (with vibration and a beep where the phone allows it) when the control room changed the route
    since the crew last confirmed it; the crew confirms with 'OK, understood'."""
    c = conn()
    day = plan["plan_date"]
    ack = db.one(c, "SELECT plan_id FROM route_ack WHERE day=? AND vehicle_id=?", (day, vehicle_id))

    def acknowledge():
        with db.tx(c):
            c.execute("INSERT INTO route_ack(day, vehicle_id, plan_id, ts, person) VALUES (?,?,?,?,?)"
                      " ON CONFLICT(day, vehicle_id) DO UPDATE SET plan_id=excluded.plan_id, ts=excluded.ts,"
                      " person=excluded.person", (day, vehicle_id, plan["id"], db.now(), person))
    if ack is None or plan.get("kind") == "extra":
        acknowledge()          # first route of the day (or the evening round): nothing to compare with
        return
    if ack["plan_id"] == plan["id"]:
        return
    old = planner.get(c, ack["plan_id"])
    ch = progress.route_changes(c, old, plan, vehicle_id)
    with st.container(border=True):
        st.error(f"### 🔔 {t('route_changed', lang)} ({hhmm(plan.get('approved_at') or plan['created_at'])})")
        if ch["added"]:
            st.markdown(f"**➕ {t('added', lang)}:** " + ", ".join(f"{x['landmark']} ({hhmm(x['arrive_ts'])})"
                                                                  for x in ch["added"]))
        if ch["removed"]:
            st.markdown(f"**➖ {t('removed', lang)}:** " + ", ".join(x["landmark"] for x in ch["removed"]))
        if st.button(t("got_it", lang), type="primary", use_container_width=True, key=f"{key}_ack_{plan['id']}"):
            acknowledge()
            st.rerun()
    st.html(ALERT_JS.replace("PLAN", str(int(plan["id"]))), unsafe_allow_javascript=True)


def _who(day: str, vehicle_id: str, lang: str, key: str) -> str | None:
    """Who is on the truck today and who is using this phone. Crews answer once per day (kept on the
    server, so a reload does not ask again); office users recording on the crew's behalf are named as such."""
    u = user()
    if u["role"] != "crew":
        return f"{u['display_name']} (office)"
    c = conn()
    cur = crews.on_truck(c, day, vehicle_id)
    editing = st.session_state.get(f"{key}_edit_crew")
    if cur and not editing:
        a, b = st.columns([3, 1])
        a.caption(f"👥 {t('crew_today', lang)}: {', '.join(cur['members'])} · 📱 {cur['holder'] or '-'}")
        if b.button(t("change", lang), key=f"{key}_chg_crew"):
            st.session_state[f"{key}_edit_crew"] = True
            st.rerun()
        st.session_state[f"{key}_person"] = cur["holder"] or cur["members"][0]
        return cur["holder"] or cur["members"][0]
    people = crews.members(c)
    own = [m["name"] for m in people if m["vehicle_id"] == vehicle_id]
    names = own + [m["name"] for m in people if m["vehicle_id"] != vehicle_id]
    with st.container(border=True):
        st.markdown(f"### 👥 {t('who_today', lang)}")
        if not names:
            st.warning(t("no_crew_list", lang))
            return None
        chosen = st.pills(t("who_today", lang), names, selection_mode="multi", label_visibility="collapsed",
                          default=(cur["members"] if cur else own) or None, key=f"{key}_crew_pick_{day}")
        holder = st.radio(t("who_phone", lang), chosen or [], index=None, key=f"{key}_holder_{day}")
        if st.button(t("start_shift", lang), type="primary", use_container_width=True, disabled=not (chosen and holder),
                     key=f"{key}_crew_save"):
            crews.set_on_truck(c, day, vehicle_id, list(chosen), holder, u["username"])
            st.session_state.pop(f"{key}_edit_crew", None)
            mark_seen()
            st.rerun()
    return None


def page() -> None:
    live_refresh(10)
    u = user()
    c = conn()
    day = progress.crew_day(c, u["vehicle_id"])
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
        # only roles that may record progress get working buttons (viewers just look)
        render(vid, progress.crew_day(c, vid), interactive=can("record_progress"), key=f"pv_{vid}")
