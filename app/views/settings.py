"""Settings: Settings & data · Vehicles & depot · Users · AI."""
from __future__ import annotations

import json

import streamlit as st

from wastewise import (audit, auth, beats, crews, db, fmt, forecast, mildura, osm, platform_info, roads, synth, telum)

from ui.common import can, conn, mark_seen, user, when
from ui.tables import Col, maps_url, table

from wastewise import area as _area

SITE_LABEL = {x.key: x.label for x in _area.SITE_TYPES}


def every_text(b: dict) -> str:
    t = "every day" if b["collect_every_days"] == 1 else f"every {b['collect_every_days']} days"
    return t + (", not on Sundays" if 6 in db.rest_days(b) else "")


def _detail_text(detail: str, places: dict[str, str] | None = None) -> str:
    try:
        d = json.loads(detail)
    except ValueError:
        return detail[:120]
    skip = {"rows", "stats", "info", "vehicles"}
    places = places or {}
    if "bin_id" in d:     # people know places by name, not by bin code
        d = {("place" if k == "bin_id" else k): (places.get(v, v) if k == "bin_id" else v) for k, v in d.items()}
    parts = [f"{k.replace('_', ' ')}: {v}" for k, v in d.items() if k not in skip and not isinstance(v, (dict, list))]
    return ", ".join(parts)[:140]


# Truck colours must never look like fill-status colours (red / orange / amber / green).
TRUCK_COLOURS = {"#2563EB": "blue", "#7C3AED": "purple", "#0D9488": "teal", "#1E3A8A": "navy",
                 "#DB2777": "pink", "#4B5563": "grey"}

ROAD_NAMES = {"motorway": "Expressway", "trunk": "Highway", "primary": "Main road", "secondary": "Secondary road",
              "tertiary": "Local main road", "unclassified": "Minor road", "residential": "Residential street",
              "living_street": "Shared street", "service": "Service lane", "road": "Other road",
              "link": "Ramp / slip road"}
IMPORTANCE = {1: "High (hospital, school, station...)", 2: "Medium", 3: "Low"}


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
                                  ("fuel_l_per_km", fuel, "fuel"),
                                  ("co2_kg_per_l_diesel", co2, "CO2"), ("map_tiles", tiles, "map")):
                    _save_setting(k, v, lbl)
                if unload != db.places(c)["landfill"]["unload_min"]:
                    with db.tx(c):
                        c.execute("UPDATE places SET unload_min=? WHERE kind='landfill'", (unload,))
                        audit.log(c, user()["username"], "landfill_unload_changed", {"minutes": unload})
                mark_seen()
        with st.expander("Typical truck speed on each kind of road"):
            with st.form("speeds"):
                cols = st.columns(3)
                new = {}
                for i, (k, v) in enumerate(s["speeds_kmh"].items()):
                    new[k] = cols[i % 3].number_input(f"{ROAD_NAMES.get(k, k)} (km/h)", 3.0, 80.0, float(v), 1.0,
                                                      key=f"spd_{k}")
                if st.form_submit_button("Save speeds"):
                    _save_setting("speeds_kmh", new, "speeds")

    if can("edit_bins"):
        st.markdown("**Bin register**")
        bins = db.bins(c, active_only=False)
        table([{"place": b["landmark"], "street": b["street"], "type": SITE_LABEL.get(b["site_type"], b["site_type"]),
                "size": fmt.litres(b["capacity_l"]), "imp": IMPORTANCE[b["priority"]].split(" (")[0],
                "sensor": "yes" if b["has_sensor"] else "no",
                "every": every_text(b),
                "use": ("In use", "green") if b["active"] else ("Not in use", "grey"), "map": (b["lat"], b["lon"])}
               for b in bins],
              [Col("place", "Place"), Col("street", "Street"), Col("type", "Type"), Col("size", "Size"),
               Col("imp", "Importance"), Col("sensor", "Sensor"), Col("every", "Collected"),
               Col("use", "", "pill"), Col("map", "", "map")], max_height=380)
        by_id = {b["id"]: b for b in bins}
        bid = st.selectbox("Change a bin", list(by_id), index=None, placeholder="choose the bin to change",
                           format_func=lambda x: f"{by_id[x]['landmark']} - {by_id[x]['street'] or ''}")
        if bid:
            b = by_id[bid]
            with st.form(f"bin_{bid}"):
                a1, a2 = st.columns(2)
                landmark = a1.text_input("Place name", b["landmark"])
                street = a2.text_input("Street", b["street"] or "")
                a1, a2, a3 = st.columns(3)
                stype = a1.selectbox("Type", list(SITE_LABEL), index=list(SITE_LABEL).index(b["site_type"])
                                     if b["site_type"] in SITE_LABEL else 0, format_func=SITE_LABEL.get)
                cap = a2.select_slider("Size (litres)", [120, 240, 360, 660, 1100, 2400, 4500],
                                       value=b["capacity_l"] if b["capacity_l"] in (120, 240, 360, 660, 1100, 2400, 4500)
                                       else 1100)
                prio = a3.radio("Importance", [1, 2, 3], index=b["priority"] - 1, format_func=IMPORTANCE.get)
                a1, a2, a3 = st.columns(3)
                every = a1.selectbox("Collect", [1, 2, 3, 7], index=[1, 2, 3, 7].index(b["collect_every_days"])
                                     if b["collect_every_days"] in (1, 2, 3, 7) else 0,
                                     format_func=lambda d: "every day" if d == 1 else f"every {d} days")
                no_sun = a1.checkbox("Not on Sundays", "6" in str(b.get("rest_days") or ""),
                                     help="For example schools and offices that are closed on Sundays.")
                sensor = a2.checkbox("Has a fill sensor", bool(b["has_sensor"]))
                active = a3.checkbox("In use", bool(b["active"]))
                if st.form_submit_button("Save this bin", type="primary"):
                    new = {"landmark": landmark, "street": street, "site_type": stype, "capacity_l": int(cap),
                           "priority": int(prio), "collect_every_days": int(every), "has_sensor": int(sensor),
                           "rest_days": "6" if no_sun else "",
                           "active": int(active)}
                    diff = {k: v for k, v in new.items() if str(v) != str(b[k])}
                    if diff:
                        with db.tx(c):
                            c.execute(f"UPDATE bins SET {', '.join(f'{k}=?' for k in diff)} WHERE id=?",
                                      (*diff.values(), bid))
                            audit.log(c, user()["username"], "bin_changed", {"bin_id": bid, **diff})
                            db.bump_version(c)
                        mark_seen()
                        st.success(f"Saved {landmark}.")
                    else:
                        st.info("Nothing changed.")

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
        names = {b["id"]: b["landmark"] for b in db.bins(c, active_only=False)}
        table([{"when": when(r["ts"]), "who": r["user"] or "system", "what": r["action"].replace("_", " "),
                "detail": _detail_text(r["detail"], names)} for r in rec],
              [Col("when", "When"), Col("who", "Who"), Col("what", "What happened"), Col("detail", "Details")],
              max_height=320)


def fleet_tab() -> None:
    c = conn()
    if not can("edit_fleet"):
        st.info("Only the administrator changes trucks and the depot.")
    vs = db.vehicles(c, active_only=False)
    table([{"name": v["name"], "size": fmt.litres(v["capacity_l"]), "colour": TRUCK_COLOURS.get(v["color"], v["color"]),
            "use": ("In service", "green") if v["active"] else ("Out of service", "grey")} for v in vs],
          [Col("name", "Truck"), Col("size", "Carries"), Col("colour", "Colour on the map"), Col("use", "", "pill")])
    if can("edit_fleet"):
        vid = st.selectbox("Change a truck", [v["id"] for v in vs], index=None, placeholder="choose the truck",
                           format_func=lambda x: next(v["name"] for v in vs if v["id"] == x))
        if vid:
            v = next(x for x in vs if x["id"] == vid)
            with st.form(f"truck_{vid}"):
                a1, a2, a3, a4 = st.columns(4)
                name = a1.text_input("Name", v["name"])
                cap = a2.number_input("Carries (litres)", 1000, 30000, int(v["capacity_l"]), 500)
                colour = a3.selectbox("Colour", list(TRUCK_COLOURS), index=list(TRUCK_COLOURS).index(v["color"])
                                      if v["color"] in TRUCK_COLOURS else 0, format_func=TRUCK_COLOURS.get)
                active = a4.checkbox("In service", bool(v["active"]))
                if st.form_submit_button("Save this truck", type="primary"):
                    with db.tx(c):
                        c.execute("UPDATE vehicles SET name=?, capacity_l=?, color=?, active=? WHERE id=?",
                                  (name, int(cap), colour, int(active), vid))
                        audit.log(c, user()["username"], "fleet_changed",
                                  {"vehicle": vid, "name": name, "capacity_l": int(cap), "active": active})
                        db.bump_version(c)
                    mark_seen()
                    st.success(f"Saved {name}.")
    p = db.places(c)
    st.markdown("**Depot and landfill**")
    for kind in ("depot", "landfill"):
        x = p[kind]
        st.markdown(f"**{x['name']}** · <a href='{maps_url(x['lat'], x['lon'])}' target='_blank'>📍 Map</a>  \n"
                    f"{x['note']}", unsafe_allow_html=True)
    if can("edit_fleet"):
        with st.form("depot"):
            a, b = st.columns(2)
            st.caption("Move the depot: copy its position from Google Maps (right-click the place → the first line "
                       "shows the two numbers).")
            lat = a.number_input("Latitude (first number)", 18.9, 19.2, float(p["depot"]["lat"]), 0.0001, format="%.5f")
            lon = b.number_input("Longitude (second number)", 72.9, 73.2, float(p["depot"]["lon"]), 0.0001, format="%.5f")
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
            from collections import Counter
            counts = dict(Counter(bm.values()).most_common())
            names = {v["id"]: v["name"] for v in db.vehicles(c, active_only=False)}
            st.write(" · ".join(f"{names.get(v, v)}: {n} bins" for v, n in counts.items()))


def crew_list() -> None:
    """The people who work on the trucks. The crew phone asks each morning who is on the truck, and every
    tap stores that person's name."""
    c = conn()
    st.markdown("**Crew members** (the names the crew phone offers at the start of each shift)")
    vn = {v["id"]: v["name"] for v in db.vehicles(c, active_only=False)}
    ms = crews.members(c, active_only=False)
    if ms:
        table([{"name": m["name"], "truck": vn.get(m["vehicle_id"], "any truck"),
                "active": ("On the list", "green") if m["active"] else ("Removed", "grey")} for m in ms],
              [Col("name", "Name"), Col("truck", "Usually on"), Col("active", "", "pill")], max_height=300)
    a, b = st.columns(2)
    with a.form("crew_add", clear_on_submit=True):
        name = st.text_input("Name")
        veh = st.selectbox("Usually on", [None] + list(vn), format_func=lambda x: "any truck" if x is None else vn[x])
        if st.form_submit_button("Add to the crew list"):
            try:
                crews.add_member(c, name, veh, user()["username"])
                st.success(f"Added {name.strip()}.")
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
    with b.form("crew_remove"):
        active = [m for m in ms if m["active"]]
        mid = st.selectbox("Remove from the list", [m["id"] for m in active], index=None,
                           format_func=lambda i: next(m["name"] for m in active if m["id"] == i),
                           placeholder="choose the person")
        if st.form_submit_button("Remove") and mid:
            crews.set_active(c, mid, False, user()["username"])
            st.rerun()


def users_tab() -> None:
    c = conn()
    if not can("manage_users"):
        st.info("Only the administrator manages users.")
        return
    us = auth.users(c)
    vn = {v["id"]: v["name"] for v in db.vehicles(c, active_only=False)}
    table([{"user": u["username"], "name": u["display_name"], "role": auth.ROLES[u["role"]],
            "truck": vn.get(u["vehicle_id"], "") if u["vehicle_id"] else "",
            "active": ("Active", "green") if u["active"] else ("Switched off", "grey")} for u in us],
          [Col("user", "Sign-in name"), Col("name", "Name"), Col("role", "Role"), Col("truck", "Truck"),
           Col("active", "", "pill")])
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
    crew_list()
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
    table([{"name": k["name"], "created": when(k["created_at"]),
            "active": ("Active", "green") if k["active"] else ("Revoked", "grey")} for k in keys],
          [Col("name", "Key name"), Col("created", "Created"), Col("active", "", "pill")])
    a, b = st.columns(2)
    with a.form("new_key", clear_on_submit=True):
        nm = st.text_input("New key name", placeholder="e.g. sensor gateway")
        if st.form_submit_button("Create key") and nm:
            k = auth.create_api_key(c, nm, actor=user()["username"])
            st.success(f"Key (shown once, stored only as a hash): `{k}`")
    with b.form("revoke", clear_on_submit=True):
        kid = st.selectbox("Revoke key", [k["id"] for k in keys if k["active"]], index=None,
                           format_func=lambda i: next(k["name"] for k in keys if k["id"] == i))
        if st.form_submit_button("Revoke") and kid:
            with db.tx(c):
                c.execute("UPDATE api_keys SET active=0 WHERE id=?", (kid,))
                audit.log(c, user()["username"], "api_key_revoked", {"id": kid})
            st.success("Revoked.")


def ai_tab() -> None:
    c = conn()
    p = platform_info.detect()
    st.markdown("**This server**")
    if p["is_s390x"]:
        st.markdown(f"- IBM LinuxONE ({p['machine_name'] or 'machine type ' + str(p['machine_type'])}), "
                    f"{p['byteorder']}-endian s390x\n"
                    f"- Telum on-chip AI accelerator: **{'present and usable' if p['nnpa_usable'] else 'present but not usable (zDNN missing)' if p['telum'] else 'not present'}**")
    else:
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
                st.write(f"In use: **{eng.device}**" + (f" for planning, **{eng.bulk.device}** for bulk forecasts"
                                                          if eng.bulk else "") + f". {eng.summary().capitalize()}.")
                if eng.timings_ms and eng.timings_ms.get("accelerator"):
                    tm = eng.timings_ms
                    table([{"batch": f"{int(n):,} rows" + (" (one planning run)" if n == "720" else " (bulk job)"),
                            "cpu": f"{tm['cpu'][n]:.2f} ms", "acc": f"{tm['accelerator'][n]:.2f} ms"}
                           for n in tm["cpu"]],
                          [Col("batch", "Measured when the model loaded"), Col("cpu", "CPU (Snap ML)"),
                           Col("acc", "Telum accelerator (Snap ML, zDNN)")])
            else:
                st.write(f"In use: native {cur['metrics'].get('gbt_library')} on the CPU"
                         + ("" if telum.snapml_version() else " (IBM Snap ML not installed here)"))
        except Exception as exc:
            st.warning(f"Could not load the inference engine: {exc}")
    if forecast.RUNS:
        table([{"job": j, "dev": r["device"], "rows": f"{r['rows']:,}", "ms": f"{r['ms']:.1f} ms", "at": when(r["ts"])}
               for j, r in sorted(forecast.RUNS.items())],
              [Col("job", "Forecast job"), Col("dev", "Ran on"), Col("rows", "Rows"), Col("ms", "Time"),
               Col("at", "When")])
    models = db.rows(c, "SELECT id, created_at, name, promoted, metrics FROM models WHERE kind='forecaster' ORDER BY id DESC")
    if models:
        rows = []
        for m in models:
            d = json.loads(m["metrics"])["decision"]
            rows.append({"trained": when(m["created_at"]), "err": f"{d['new_mae']:.1f}% of the bin",
                         "base": f"{d['best_baseline_mae']:.1f}% of the bin",
                         "gain": f"{(1 - d['new_mae'] / d['best_baseline_mae']) * 100:.0f}% better",
                         "use": ("In use", "green") if m["promoted"] else ("Not used", "grey")})
        table(rows, [Col("trained", "Trained"), Col("err", "Average forecast error (per 3 hours)"),
                     Col("base", "Simple rule's error"), Col("gain", "AI vs simple rule"), Col("use", "", "pill")])
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
    model_name = (stt["model"] or "").replace("\\", "/").split("/")[-1]
    st.write(("**Running on this server**" + (f" ({model_name})" if model_name else "") +
              ". Every figure it writes is checked against the plan.") if stt["ok"] else
             "**Not running** - briefings and driver messages use the built-in templates (always correct).")
    sc = llm.selfcheck_result(c)
    if stt["ok"]:
        st.markdown("**Self-check** - before WasteWise offers Granite for a task, it gives it one test prompt for "
                    "that task and checks every figure in the answer. Only tasks that pass get a "
                    "'Let Granite reword it' button; the others keep the templates.")
        if sc and sc.get("model") == stt.get("model") and sc.get("kinds"):
            names = {"briefing": "Morning briefing (English)", "driver_en": "Crew message (English)",
                     "driver_hi": "Crew message (Hindi)", "driver_mr": "Crew message (Marathi)"}
            table([{"task": names.get(k, k), "res": ("Passed", "green") if v["ok"] else ("Not offered", "grey"),
                    "why": "" if v["ok"] else (v.get("why") or "").replace("Granite's text ", "its text ")
                                                    .replace(" - showing the checked template instead", ""),
                    "secs": f"{v['seconds']:.0f} s"} for k, v in sc["kinds"].items()],
                  [Col("task", "Task"), Col("res", "", "pill"), Col("why", "Why not"), Col("secs", "Time taken")])
            st.caption(f"Checked {when(sc['ts'])}. A base (not instruction-tuned) model usually fails: it does not "
                       "follow instructions, so its figures do not match the plan.")
            with st.expander("What the model wrote in the test"):
                for k, v in sc["kinds"].items():
                    st.markdown(f"**{k}**")
                    st.text(v["sample"])
        else:
            st.caption("The self-check runs by itself a few minutes after the model starts.")
        if can("settings") and st.button("Run the self-check now", help="About 1-4 minutes on this server."):
            with st.spinner("Testing the language model ..."):
                llm.selfcheck(c)
            st.rerun()
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
    from views import roads_view
    st.title("Settings")
    t = st.tabs(["Settings & data", "Vehicles & depot", "Roads", "Users", "AI"])
    with t[0]:
        settings_tab()
    with t[1]:
        fleet_tab()
        truck_log()
    with t[2]:
        roads_view.tab()
    with t[3]:
        users_tab()
    with t[4]:
        ai_tab()


def truck_log() -> None:
    """Fuel, maintenance and breakdowns. 'Out of service until' keeps the truck out of the plans."""
    from datetime import datetime, timedelta

    from wastewise import config, fleet
    c = conn()
    st.markdown("**Truck log** (fuel, maintenance, breakdowns)")
    vs = {v["id"]: v["name"] for v in db.vehicles(c, active_only=False)}
    off = fleet.out_of_service(c, db.now())
    if off:
        for vid, r in off.items():
            st.warning(f"**{vs.get(vid, vid)}** is out of service: {fleet.reason_text(r)}. Plans leave it out.",
                       icon=":material/build:")
    rows = fleet.entries(c, limit=60)
    if rows:
        table([{"when": when(r["ts"]), "truck": vs.get(r["vehicle_id"], r["vehicle_id"]),
                "what": fleet.KINDS.get(r["kind"], r["kind"]),
                "fuel": fmt.litres(r["litres"]) if r["litres"] else "",
                "cost": f"Rs {r['cost_rs']:,.0f}" if r["cost_rs"] else "",
                "km": f"{r['odometer_km']:,.0f} km" if r["odometer_km"] else "",
                "out": (f"out of service until {when(r['out_until'])}" if r["out_until"] else
                        "out of service" if r["out_from"] else ""),
                "note": r["note"] or "", "who": r["user"] or ""} for r in rows],
              [Col("when", "When"), Col("truck", "Truck"), Col("what", "What"), Col("fuel", "Diesel"),
               Col("cost", "Cost"), Col("km", "Odometer"), Col("out", "Service"), Col("note", "Note"),
               Col("who", "By")], max_height=320)
    if not can("edit_fleet") and not can("edit_bins"):
        return
    with st.form("truck_log", clear_on_submit=True):
        a, b = st.columns(2)
        vid = a.selectbox("Truck", list(vs), format_func=vs.get)
        kind = b.radio("What", list(fleet.KINDS), format_func=fleet.KINDS.get, horizontal=True)
        a, b, d = st.columns(3)
        litres = a.number_input("Diesel (litres)", 0.0, 500.0, 0.0, 1.0)
        cost = b.number_input("Cost (Rs)", 0.0, 1e6, 0.0, 100.0)
        odo = d.number_input("Odometer (km)", 0.0, 1e7, 0.0, 1.0)
        note = st.text_input("Note", placeholder="e.g. brake repair, tyre change")
        a, b, d = st.columns(3)
        out = a.checkbox("Out of service from now")
        now = datetime.now(config.IST)
        until_d = b.date_input("until (date)", value=(now + timedelta(days=1)).date())
        until_t = d.time_input("until (time)", value=datetime.strptime("06:00", "%H:%M").time(), step=1800)
        if st.form_submit_button("Save to the log", type="primary"):
            try:
                until = int(datetime.combine(until_d, until_t, tzinfo=config.IST).timestamp()) if out else None
                fleet.add(c, vid, kind, user()["username"], litres or None, cost or None, odo or None, note,
                          out_from=db.now() if out else None, out_until=until)
                mark_seen()
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
