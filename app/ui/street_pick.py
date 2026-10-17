"""Tap-to-choose street map for road closures.

Drawn as SVG from our own OpenStreetMap extract (no map tiles, no binary data, so it works the same on
big-endian IBM LinuxONE). Tapping a street, or its dot, sends the street's name back to the page.
"""
from __future__ import annotations

import streamlit as st

_JS = """
export default function(component) {
  const { data, setTriggerValue, parentElement } = component;
  const box = parentElement.querySelector('#ww-streets');
  const [x0, y0, x1, y1] = data.bounds;
  const W = 1000, H = Math.round(1000 * (y1 - y0) / (x1 - x0) * 1.06);
  const px = (lon) => ((lon - x0) / (x1 - x0) * W).toFixed(1);
  const py = (lat) => ((y1 - lat) / (y1 - y0) * H).toFixed(1);
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.setAttribute('style', 'width:100%;height:auto;background:#fff;border:1px solid #E2E8F0;border-radius:8px;touch-action:manipulation');
  const tip = parentElement.querySelector('#ww-tip');
  data.streets.forEach((s) => {
    const colour = s.closed ? '#DC2626' : (s.name === data.chosen ? '#1D4ED8' : '#94A3B8');
    const width = s.closed || s.name === data.chosen ? 7 : 3;
    s.parts.forEach((part) => {
      const pl = document.createElementNS(ns, 'polyline');
      pl.setAttribute('points', part.map(p => `${px(p[1])},${py(p[0])}`).join(' '));
      pl.setAttribute('fill', 'none'); pl.setAttribute('stroke', colour); pl.setAttribute('stroke-width', width);
      pl.setAttribute('stroke-linecap', 'round');
      svg.appendChild(pl);
      const hit = document.createElementNS(ns, 'polyline');     // wide invisible line: easy to tap
      hit.setAttribute('points', pl.getAttribute('points'));
      hit.setAttribute('fill', 'none'); hit.setAttribute('stroke', 'transparent'); hit.setAttribute('stroke-width', 22);
      hit.style.cursor = 'pointer';
      hit.addEventListener('click', () => setTriggerValue('picked', s.name));
      hit.addEventListener('mouseenter', () => { tip.textContent = s.name; });
      svg.appendChild(hit);
    });
    const dot = document.createElementNS(ns, 'circle');
    dot.setAttribute('cx', px(s.mid[1])); dot.setAttribute('cy', py(s.mid[0])); dot.setAttribute('r', 9);
    dot.setAttribute('fill', colour); dot.setAttribute('stroke', '#fff'); dot.setAttribute('stroke-width', 2);
    dot.style.cursor = 'pointer';
    dot.setAttribute('data-street', s.name);
    dot.addEventListener('click', () => setTriggerValue('picked', s.name));
    dot.addEventListener('mouseenter', () => { tip.textContent = s.name; });
    svg.appendChild(dot);
  });
  box.replaceChildren(svg);
}
"""

_component = st.components.v2.component(
    "ww_street_pick", html="<div id='ww-streets'></div><div id='ww-tip' style='font-size:0.85rem;color:#334155;"
                           "min-height:1.2em'></div>", js=_JS)


def pick(streets: dict[str, dict], chosen: str | None, closed: set[str], key: str = "street_pick") -> str | None:
    """Shows the map; returns the street the user just tapped (or None)."""
    items = []
    for name, s in streets.items():
        line = s["line"]
        parts, cur = [], []
        for a, b in zip(line, line[1:] + [None]):
            cur.append([round(a[0], 5), round(a[1], 5)])
            if b is None or abs(a[0] - b[0]) + abs(a[1] - b[1]) > 0.003:   # separate pieces of the street
                if len(cur) > 1:
                    parts.append(cur)
                cur = []
        items.append({"name": name, "parts": parts, "mid": [s["lat"], s["lon"]], "closed": name in closed})
    items.sort(key=lambda x: (x["closed"] or x["name"] == chosen))       # highlighted streets drawn on top
    res = _component(key=key, data={"streets": items, "chosen": chosen, "bounds": [73.000, 19.045, 73.024, 19.071]},
                     on_picked_change=lambda: None)
    try:
        return res.get("picked") if hasattr(res, "get") else getattr(res, "picked", None)
    except Exception:
        return None
