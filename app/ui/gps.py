"""The crew phone's position, from the browser (only with the crew's permission).

A small Streamlit (components v2) element asks the browser for the position and keeps watching it while
the page is open. It reports a new fix at most every 20 seconds, or sooner when the truck has moved 25 m
or the fix became much more precise. Browsers allow this only on HTTPS pages (the public link) or on
localhost. Nothing is sent anywhere except to this server.
"""
from __future__ import annotations

import time

import streamlit as st

from wastewise import proof

_JS = """
export default function(component) {
  const { setStateValue, parentElement } = component;
  const out = parentElement.querySelector('#ww-gps');
  if (!('geolocation' in navigator) || !window.isSecureContext) {
    setStateValue('fix', {error: 'unavailable'});
    return;
  }
  let last = null, lastSent = 0, n = 0;
  const far = (a, b) => {
    const R = 6371000, r = Math.PI / 180;
    const x = (b.lon - a.lon) * r * Math.cos((a.lat + b.lat) * r / 2), y = (b.lat - a.lat) * r;
    return Math.sqrt(x * x + y * y) * R;
  };
  const id = navigator.geolocation.watchPosition((p) => {
    const fix = {lat: p.coords.latitude, lon: p.coords.longitude, acc: Math.round(p.coords.accuracy)};
    const now = Date.now();
    if (!last || now - lastSent > 20000 || far(last, fix) > 25 || fix.acc < last.acc * 0.6) {
      last = fix; lastSent = now;
      setStateValue('fix', {...fix, n: ++n});
    }
  }, (e) => setStateValue('fix', {error: e.code === 1 ? 'denied' : 'unavailable'}),
  {enableHighAccuracy: true, maximumAge: 15000, timeout: 30000});
  // a parked phone may not report new positions: repeat the last one every minute so it stays usable
  const timer = setInterval(() => { if (last) { lastSent = Date.now(); setStateValue('fix', {...last, n: ++n}); } },
                            60000);
  return () => { navigator.geolocation.clearWatch(id); clearInterval(timer); };
}
"""

_component = st.components.v2.component("ww_gps", html="<span id='ww-gps'></span>", js=_JS)


def _received(key: str) -> None:
    st.session_state[f"{key}_fix_at"] = time.time()


def watch(key: str = "gps") -> tuple[dict | None, str]:
    """Mount the position watcher. Returns (usable fix {lat, lon, acc, ts} or None, state) where state is
    'ok', 'waiting', 'denied' (the crew said no) or 'unavailable' (no GPS, or not an HTTPS page)."""
    res = _component(key=key, on_fix_change=lambda: _received(key), height=0)
    fix = None
    try:
        fix = res.get("fix") if hasattr(res, "get") else getattr(res, "fix", None)
    except Exception:
        fix = None
    if not fix:
        return None, "waiting"
    if fix.get("error"):
        return None, fix["error"]
    usable = proof.usable_fix(fix, st.session_state.get(f"{key}_fix_at"), time.time())
    return usable, ("ok" if usable else "waiting")
