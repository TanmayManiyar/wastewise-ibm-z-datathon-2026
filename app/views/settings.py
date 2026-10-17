"""Settings: Settings & data · Vehicles & depot · Users · AI."""
from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from wastewise import (audit, auth, beats, db, forecast, mildura, osm, platform_info, roads, synth, telum)

from ui.common import can, conn, mark_seen, user, when

# Truck colours must never look like fill-status colours (red / orange / amber / green).
TRUCK_COLOURS = {"#2563EB": "blue", "#7C3AED": "purple", "#0D9488": "teal", "#1E3A8A": "navy",
                 "#DB2777": "pink", "#4B5563": "grey"}


def _save_setting(key, value, label):
    c = conn()
    old = db.get_settings(c).get(key)
    if old != value:
        with db.tx(c):
            db.set_setting(c, key, value)
            audit.log(c, user()["username"], "setting_changed", {"key": key, "from": old, "to": value})
            db.bump_version(c)
        st.toast(f"Saved: {label}")


def settings_tab() -> None:
    c = conn()
    s = db.get_settings(c)
    if can("settings"):
        with st.form("settings"):
            st.markdown("**Shift and work times**")
            a, b, d = st.columns(3)
            shift_start = a.text_input("Shift starts (HH:MM)", s["shift_start"])
            shift_hours = b.number_input("Shift length (hours)", 1.0, 12.0, float(s["shift_hours"]), 0.5)
            unload = d.number_input("Unloading at the landfill (min)", 5.0, 90.0,
                                    float(db.places(c)["landfill"]["unload_min"]), 1.0)
            a, b, d = st.columns(3)
            sb = a.number_input("Time per bin (min)", 0.5, 15.0, float(s["service_min_base"]), 0.5)
            sk = b.number_input("Extra time per 1000 L (min)", 0.0, 10.0, float(s["service_min_per_1000l"]), 0.5)
            late = d.number_input("Late after (min)", 5, 180, int(s["late_threshold_min"]), 5)
            st.markdown("**Planning**")
            a, b, d = st.columns(3)
            thr = a.slider("Add a bin that is not due when it will be this full (%)", 40, 100,
                           int(s["collect_threshold_pct"]), 5)
            bp = b.number_input("Cost of serving another truck's area (minutes)", 0.0, 120.0,
                                float(s["beat_penalty_min"]), 1.0)
            secs = d.slider("Optimiser time (seconds)", 2, 30, int(s["optimiser_seconds"]))
            a, b, d = st.columns(3)
            fuel = a.number_input("Fuel use (L per km)", 0.05, 2.0, float(s["fuel_l_per_km"]), 0.01)
            co2 = b.number_input("CO₂ per litre of diesel (kg)", 1.0, 4.0, float(s["co2_kg_per_l_diesel"]), 0.01)
            tiles = d.radio("Map background", ["osm", "offline"], index=["osm", "offline"].index(s["map_tiles"]),
                            format_func={"osm": "OpenStreetMap tiles (loaded by your browser)",
                                         "offline": "Offline (streets drawn from our own extract)"}.get)
            if st.form_submit_button("Save settings", type="primary"):
                try:
                    hh, mm = map(int, shift_start.split(":"))
                    assert 0 <= hh < 24 and 0 <= mm < 60
                except (ValueError, AssertionError):
                    st.error("Shift start must look like 06:00")
                    return
                for k, v, lbl in (("shift_start", f"{hh:02d}:{mm:02d}", "shift start"),
                                  ("shift_hours", shift_hours, "shift length"), ("service_min_base", sb, "time per bin"),
                                  ("service_min_per_1000l", sk, "time per 1000 L"), ("late_threshold_min", late, "late"),
                                  ("collect_threshold_pct", float(thr), "threshold"), ("beat_penalty_min", bp, "beats"),
                                  ("optimiser_seconds", float(secs), "optimiser time"), ("fuel_l_per_km", fuel, "fuel"),
                                  ("co2_kg_per_l_diesel", co2, "CO2"), ("map_tiles", tiles, "map")):
                    _save_setting(k, v, lbl)
                if unload != db.places(c)["landfill"]["unload_min"]:
                    with db.tx(c):
                        c.execute("UPDATE places SET unload_min=? WHERE kind='landfill'", (unload,))
                        audit.log(c, user()["username"], "landfill_unload_changed", {"minutes": unload})
                mark_seen()
        with st.expander("Truck speeds by road type (km/h)"):
            sp = pd.DataFrame([{"road type": k, "km/h": v} for k, v in s["speeds_kmh"].items()])
            ed = st.data_editor(sp, hide_index=True, disabled=["road type"], key="speeds")
            if st.button("Save speeds"):
                _save_setting("speeds_kmh", {r["road type"]: float(r["km/h"]) for _, r in ed.iterrows()}, "speeds")

    if can("edit_bins"):
        st.markdown("**Bin register**")
        bins = db.bins(c, active_only=False)
        df = pd.DataFrame(bins)[["id", "landmark", "street", "site_type", "capacity_l", "priority", "has_sensor",
                                 "active", "collect_every_days"]]
        ed = st.data_editor(df, hide_index=True, disabled=["id"], key="bins_editor", use_container_width=True,
                            column_config={"has_sensor": st.column_config.CheckboxColumn("sensor"),
                                           "active": st.column_config.CheckboxColumn("active"),
                                           "priority": st.column_config.NumberColumn(min_value=1, max_value=3, step=1),
                                           "collect_every_days": st.column_config.NumberColumn("every (days)",
                                                                                               min_value=1, max_value=7),
                                           "capacity_l": st.column_config.NumberColumn("capacity (L)", min_value=60,
                                                                                       max_value=10000, step=60)})
        if st.button("Save bin register"):
            changes = 0
            with db.tx(c):
                for (_, new), old in zip(ed.iterrows(), bins):
                    diff = {k: new[k] for k in ("landmark", "street", "site_type", "capacity_l", "priority",
                                                "has_sensor", "active", "collect_every_days")
                            if str(new[k]) != str(old[k])}
                    if diff:
                        sets = ", ".join(f"{k}=?" for k in diff)
                        vals = [int(v) if isinstance(v, (bool,)) else (v.item() if hasattr(v, "item") else v)
                                for v in diff.values()]
                        c.execute(f"UPDATE bins SET {sets} WHERE id=?", (*vals, old["id"]))
                        audit.log(c, user()["username"], "bin_changed", {"bin_id": old["id"], **{k: str(v) for k, v in diff.items()}})
                        changes += 1
                db.bump_version(c)
            mark_seen()
            st.success(f"{changes} bin(s) updated.")

    if can("settings"):
        st.markdown("**Data**")
        info = db.get_meta(c, "synthetic_info")
        if info:
            i = json.loads(info)
            st.write(f"Demonstration history: **synthetic**, {i['days']} days up to {when(i['end_ts'])}, "
                     f"{i['rows']['readings']:,} readings, {i['rows']['events']:,} collections.")
        if st.button("Refresh demo data to now", help="Regenerates the synthetic history so it ends now. Keeps "
                     "records entered by people or the API, settings, bins, trucks and users."):
            with st.spinner("Generating 180 days of synthetic history ..."):
                i = synth.generate(c)
            audit.log(c, user()["username"], "synthetic_refreshed", {"rows": i["rows"]})
            mark_seen()
            st.success(f"Done: {i['rows']['readings']:,} synthetic readings up to now.")
        prov = db.get_meta(c, "osm_provenance")
        if prov:
            p = json.loads(prov)
            st.caption(f"Streets and landmarks: OpenStreetMap extract of {p['roads']['osm_base']} "
                       f"({p['roads']['elements']:,} road elements, {p['pois']['elements']} landmarks). "
                       f"{p['roads']['licence']}.")
        mp = mildura.provenance()
        if mp:
            st.caption(f"Real-world test data: {mp['dataset']} ({mp['publisher']}, {mp['licence']}), "
                       f"downloaded {when(mp['downloaded_at'])}, SHA-256 {mp['sha256'][:12]}…")
        st.markdown("**Audit log**")
        if st.button("Check audit log integrity"):
            r = audit.verify(c)
            (st.success if r["ok"] else st.error)(
                f"{'Intact' if r['ok'] else 'TAMPERED'}: {r['checked']} entries checked"
                + ("" if r["ok"] else f", first bad entry #{r['first_bad_id']}"))
        rec = audit.recent(c, 100)
        st.dataframe(pd.DataFrame([{"#": r["id"], "When": when(r["ts"]), "Who": r["user"], "What": r["action"],
                                    "Details": r["detail"][:160]} for r in rec]), hide_index=True,
                     use_container_width=True, height=260)


def fleet_tab() -> None:
    c = conn()
    if not can("edit_fleet"):
        st.info("Only the administrator changes trucks and the depot.")
    vs = db.vehicles(c, active_only=False)
    df = pd.DataFrame(vs)[["id", "name", "capacity_l", "color", "active"]]
    ed = st.data_editor(df, hide_index=True, disabled=["id"] if can("edit_fleet") else True, key="fleet",
                        column_config={"color": st.column_config.SelectboxColumn("colour", options=list(TRUCK_COLOURS)),
                                       "active": st.column_config.CheckboxColumn("in service"),
                                       "capacity_l": st.column_config.NumberColumn("capacity (L)", min_value=1000,
                                                                                   max_value=30000, step=500)})
    if can("edit_fleet") and st.button("Save trucks"):
        with db.tx(c):
            for _, r in ed.iterrows():
                c.execute("UPDATE vehicles SET name=?, capacity_l=?, color=?, active=? WHERE id=?",
                          (r["name"], int(r["capacity_l"]), r["color"], int(bool(r["active"])), r["id"]))
            audit.log(c, user()["username"], "fleet_changed", {"vehicles": ed.to_dict("records")})
            db.bump_version(c)
        mark_seen()
        st.success("Trucks saved.")
    p = db.places(c)
    st.markdown("**Depot and landfill**")
    for kind in ("depot", "landfill"):
        x = p[kind]
        st.write(f"**{x['name']}** · {x['lat']:.5f}, {x['lon']:.5f}  \n{x['note']}")
    if can("edit_fleet"):
        with st.form("depot"):
            a, b = st.columns(2)
            lat = a.number_input("Depot latitude", 18.9, 19.2, float(p["depot"]["lat"]), 0.0001, format="%.5f")
            lon = b.number_input("Depot longitude", 72.9, 73.2, float(p["depot"]["lon"]), 0.0001, format="%.5f")
            name = st.text_input("Depot name", p["depot"]["name"])
            if st.form_submit_button("Save depot"):
                with db.tx(c):
                    c.execute("UPDATE places SET lat=?, lon=?, name=? WHERE kind='depot'", (lat, lon, name))
                    audit.log(c, user()["username"], "depot_changed", {"lat": lat, "lon": lon, "name": name})
                    db.bump_version(c)
                st.success("Depot saved. Travel times are recomputed at the next plan.")
        st.markdown("**Vehicle areas (beats)**")
        st.caption("Each truck's area is a slice around the depot, sized to its capacity.")
        if st.button("Recompute areas"):
            from wastewise import state
            net = roads.network_for(c)
            prof = state.rate_profiles(c, db.now())
            bins = db.bins(c)
            daily = {b["id"]: float(prof[b["id"]].sum() / 7 * b["capacity_l"] / 100) for b in bins}
            bm = beats.compute(bins, daily, db.places(c)["depot"], db.vehicles(c), net.time_s,
                               {s: i for i, s in enumerate(net.stop_ids)})
            beats.save(c, bm, user()["username"])
            mark_seen()
            st.success("Areas recomputed.")
        bm = beats.load(c)
        if bm:
            counts = pd.Series(bm).value_counts()
            st.write(" · ".join(f"{v}: {n} bins" for v, n in counts.items()))


def users_tab() -> None:
    c = conn()
    if not can("manage_users"):
        st.info("Only the administrator manages users.")
        return
    us = auth.users(c)
    st.dataframe(pd.DataFrame([{"User": u["username"], "Name": u["display_name"], "Role": auth.ROLES[u["role"]],
                                "Truck": u["vehicle_id"] or "", "Active": bool(u["active"])} for u in us]),
                 hide_index=True, use_container_width=True)
    a, b = st.columns(2)
    with a.form("new_user", clear_on_submit=True):
        st.markdown("**Add a user**")
        un = st.text_input("User name")
        dn = st.text_input("Full name")
        role = st.selectbox("Role", list(auth.ROLES), format_func=auth.ROLES.get)
        veh = st.selectbox("Truck (crew only)", [None] + [v["id"] for v in db.vehicles(c)])
        if st.form_submit_button("Create"):
            try:
                pw = auth.create_user(c, un.strip(), dn.strip() or un, role, veh if role == "crew" else None,
                                      actor=user()["username"])
                st.success(f"Created. First password (shown once - give it to the person): `{pw}`")
            except Exception as exc:
                st.error(f"Could not create: {exc}")
    with b.form("reset", clear_on_submit=True):
        st.markdown("**Reset a password / deactivate**")
        who = st.selectbox("User", [u["username"] for u in us], index=None)
        act = st.radio("Action", ["new password", "deactivate", "activate"], horizontal=True)
        if st.form_submit_button("Apply") and who:
            if act == "new password":
                pw = auth.generate_password()
                auth.set_password(c, who, pw, actor=user()["username"])
                st.success(f"New password for {who} (shown once): `{pw}`")
            else:
                with db.tx(c):
                    c.execute("UPDATE users SET active=? WHERE username=?", (int(act == "activate"), who))
                    audit.log(c, user()["username"], f"user_{act}d", {"username": who})
                st.success(f"{who}: {act}d")
    with st.expander("Your own password"):
        with st.form("own_pw", clear_on_submit=True):
            p1 = st.text_input("New password", type="password")
            p2 = st.text_input("Repeat", type="password")
            if st.form_submit_button("Change"):
                if len(p1) < 10 or p1 != p2:
                    st.error("At least 10 characters, typed the same twice.")
                else:
                    auth.set_password(c, user()["username"], p1)
                    st.success("Changed.")
    st.markdown("**API keys** (for sensors and other systems)")
    keys = db.rows(c, "SELECT id, name, created_at, active FROM api_keys ORDER BY id")
    st.dataframe(pd.DataFrame([{"#": k["id"], "Name": k["name"], "Created": when(k["created_at"]),
                                "Active": bool(k["active"])} for k in keys]), hide_index=True)
    a, b = st.columns(2)
    with a.form("new_key", clear_on_submit=True):
        nm = st.text_input("New key name", placeholder="e.g. sensor gateway")
        if st.form_submit_button("Create key") and nm:
            k = auth.create_api_key(c, nm, actor=user()["username"])
            st.success(f"Key (shown once, stored only as a hash): `{k}`")
    with b.form("revoke", clear_on_submit=True):
        kid = st.selectbox("Revoke key", [k["id"] for k in keys if k["active"]], index=None)
        if st.form_submit_button("Revoke") and kid:
            with db.tx(c):
                c.execute("UPDATE api_keys SET active=0 WHERE id=?", (kid,))
                audit.log(c, user()["username"], "api_key_revoked", {"id": kid})
            st.success("Revoked.")


def ai_tab() -> None:
    c = conn()
    p = platform_info.detect()
    st.markdown("**Machine**")
    st.write(f"Architecture **{p['arch']}** ({p['byteorder']}-endian) · machine type {p['machine_type'] or '-'} "
             f"{'(' + p['machine_name'] + ')' if p['machine_name'] else ''} · NNPA flag "
             f"{'yes' if p['nnpa_feature'] else 'no'} · zDNN {p['zdnn'] or 'not installed'} · accelerator usable: "
             f"**{'yes' if p['nnpa_usable'] else 'no'}**")
    if not p["is_s390x"]:
        st.info("This is not an IBM LinuxONE machine, so there is no Telum accelerator here. The same code uses it "
                "automatically on IBM z16 / z17 (LinuxONE 4 / 5).")
    s = db.get_settings(c)
    st.markdown("**Forecaster tree inference**")
    mode = st.radio("Run the boosted trees on", ["auto", "cpu", "accelerator"],
                    index=["auto", "cpu", "accelerator"].index(s["telum_inference"]), horizontal=True,
                    format_func={"auto": "auto (time both, use the faster)", "cpu": "CPU",
                                 "accelerator": "Telum AI accelerator"}.get, disabled=not can("settings"))
    if mode != s["telum_inference"]:
        _save_setting("telum_inference", mode, "inference engine")
        forecast._CACHE.clear()
    cur = forecast.promoted(c)
    if cur:
        try:
            cache = forecast._inference_model(c)
            eng = cache.get("engine") if cache else None
            if eng:
                st.write(f"In use: **{eng.device}**" + (f" · measured at load: {eng.timings_ms}" if eng.timings_ms else ""))
                st.session_state._engine_label = "trees on " + ("Telum" if eng.tree_format == "zdnn_tensors" else "CPU")
            else:
                st.write(f"In use: native {cur['metrics'].get('gbt_library')} on the CPU"
                         + ("" if telum.snapml_version() else " (IBM Snap ML not installed here)"))
        except Exception as exc:
            st.warning(f"Could not load the inference engine: {exc}")
    models = db.rows(c, "SELECT id, created_at, name, promoted, metrics FROM models WHERE kind='forecaster' ORDER BY id DESC")
    if models:
        st.dataframe(pd.DataFrame([{"#": m["id"], "Trained": when(m["created_at"]), "Best": m["name"],
                                    "Error (3 h)": json.loads(m["metrics"])["decision"]["new_mae"],
                                    "Best baseline": json.loads(m["metrics"])["decision"]["best_baseline_mae"],
                                    "Promoted": bool(m["promoted"])} for m in models]), hide_index=True)
    if can("settings") and st.button("Train the forecaster now", help="Trains on the history, tests on the locked "
                                     "test window and promotes the new model only if it is more accurate."):
        with st.spinner("Training (about 30 seconds) ..."):
            m = forecast.train_and_register(c, user()["username"])
        d = m["decision"]
        (st.success if d["promoted"] else st.info)(
            f"New model error {d['new_mae']} vs baseline {d['best_baseline_mae']}"
            + (f" and current model {d['current_promoted_mae']}" if d["current_promoted_mae"] else "")
            + (" - promoted." if d["promoted"] else " - not better, kept the current model."))
    st.markdown("**Language model (IBM Granite, local)**")
    from wastewise import llm
    stt = llm.status(c)
    st.write(f"Server {stt['url']} · model {stt['model']} · "
             + ("**reachable**" if stt["ok"] else f"not reachable ({stt['error']}) - briefings use the built-in "
                                                  "templates, which are always available."))
    if can("settings"):
        with st.form("llm"):
            url = st.text_input("Local llama.cpp server", s.get("llm_url", llm.DEFAULT_URL))
            on = st.checkbox("Use the language model for briefings and messages", bool(s.get("llm_enabled", True)))
            if st.form_submit_button("Save"):
                if not llm.is_local(url):
                    st.error("Only a server on this machine (localhost) is allowed - no data leaves the server.")
                else:
                    _save_setting("llm_url", url, "LLM server")
                    _save_setting("llm_enabled", on, "LLM on/off")


def page() -> None:
    st.title("Settings")
    t = st.tabs(["Settings & data", "Vehicles & depot", "Users", "AI"])
    with t[0]:
        settings_tab()
    with t[1]:
        fleet_tab()
    with t[2]:
        users_tab()
    with t[3]:
        ai_tab()
