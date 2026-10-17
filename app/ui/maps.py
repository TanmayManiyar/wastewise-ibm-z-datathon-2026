"""Plotly maps of plans and bins.

Route lines and the outer ring of each stop use the truck's colour (blue /
purple / teal - never red, orange, amber or green). The centre of each bin
marker shows its fill status: red = full at shift start, orange = full by the
time the truck arrives, amber = over half full, green = fine.
Tiles: OpenStreetMap raster tiles loaded by the viewer's browser, or an
offline map that draws the streets from WasteWise's own OSM extract.
"""
from __future__ import annotations

import numpy as np
import plotly.graph_objects as go

from wastewise import osm, roads

from wastewise import fmt

from .common import STATUS_COLOURS, STATUS_WORDS, hhmm

DEPOT_COLOUR = "#334155"
LANDFILL_COLOUR = "#111827"
NOT_PLANNED = "#94A3B8"


def _fill_words(pct: float) -> str:
    f = fmt.fill(pct)
    return f if f == "overflowing" else f"about {f} full"


def _street_lines(net: roads.Network, max_edges: int = 20000):
    g = net.graph
    lat, lon = [], []
    seen = set()
    for u, adj in enumerate(g.adj):
        for v, _, _ in adj:
            if (v, u) in seen:
                continue
            seen.add((u, v))
            lat += [g.lat[u], g.lat[v], None]
            lon += [g.lon[u], g.lon[v], None]
            if len(seen) > max_edges:
                break
    return lat, lon


def _layout(fig: go.Figure, center, zoom: float, height: int, tiles: str) -> go.Figure:
    style = "open-street-map" if tiles == "osm" else "white-bg"
    fig.update_layout(map=dict(style=style, center=dict(lat=center[0], lon=center[1]), zoom=zoom),
                      margin=dict(l=0, r=0, t=0, b=0), height=height,
                      legend=dict(orientation="h", yanchor="bottom", y=0.01, xanchor="left", x=0.01,
                                  bgcolor="rgba(255,255,255,0.85)"),
                      uirevision="keep")
    return fig


def _base(net: roads.Network | None, tiles: str) -> go.Figure:
    fig = go.Figure()
    if tiles != "osm" and net is not None:
        la, lo = _street_lines(net)
        fig.add_trace(go.Scattermap(lat=la, lon=lo, mode="lines", line=dict(width=1, color="#CBD5E1"),
                                    hoverinfo="skip", showlegend=False))
    return fig


def _places(fig: go.Figure, places: dict) -> None:
    for kind, colour, label in (("depot", DEPOT_COLOUR, "Depot"), ("landfill", LANDFILL_COLOUR, "Landfill")):
        p = places.get(kind)
        if not p:
            continue
        fig.add_trace(go.Scattermap(lat=[p["lat"]], lon=[p["lon"]], mode="markers+text",
                                    marker=dict(size=20, color=colour), text=[label], textposition="bottom center",
                                    textfont=dict(size=13, color=colour), name=label,
                                    hovertext=[p["name"]], hoverinfo="text"))


def plan_map(plan: dict, places: dict, bins: list[dict], net: roads.Network | None = None,
             vehicles: list[str] | None = None, tiles: str = "osm", height: int = 620,
             highlight: str | None = None) -> go.Figure:
    fig = _base(net, tiles)
    planned = set()
    for r in plan["routes"]:
        if vehicles and r["vehicle_id"] not in vehicles:
            continue
        if r["geometry"]:
            g = np.array(r["geometry"])
            fig.add_trace(go.Scattermap(lat=g[:, 0], lon=g[:, 1], mode="lines",
                                        line=dict(width=4, color=r["color"]), name=r["vehicle_name"],
                                        hoverinfo="skip"))
        stops = [s for s in r["stops"] if s["kind"] == "bin"]
        planned |= {s["bin_id"] for s in stops}
        if not stops:
            continue
        lat = [s["lat"] for s in stops]
        lon = [s["lon"] for s in stops]
        nums = [str(i) for i in range(1, len(stops) + 1)]
        hover = [f"<b>{s['landmark']}</b><br>{s['street']}<br>{r['vehicle_name']}, stop {n}, arrives "
                 f"{hhmm(s['arrive_ts'])}<br>{_fill_words(s['expected_fill_pct']).capitalize()} · "
                 f"{STATUS_WORDS[s['status']]}"
                 for n, s in zip(nums, stops)]
        fig.add_trace(go.Scattermap(lat=lat, lon=lon, mode="markers", marker=dict(size=24, color=r["color"]),
                                    hoverinfo="skip", showlegend=False))
        fig.add_trace(go.Scattermap(lat=lat, lon=lon, mode="markers+text",
                                    marker=dict(size=14, color=[STATUS_COLOURS[s["status"]] for s in stops]),
                                    text=nums, textposition="top right", textfont=dict(size=14, color="#0F172A"),
                                    hovertext=hover, hoverinfo="text", showlegend=False))
    left = {u["bin_id"]: u for u in plan.get("unserved", [])}
    other = [b for b in bins if b["id"] not in planned]
    if other:
        fig.add_trace(go.Scattermap(
            lat=[b["lat"] for b in other], lon=[b["lon"] for b in other], mode="markers",
            marker=dict(size=11, color=["#111827" if b["id"] in left and left[b["id"]]["due"] else NOT_PLANNED
                                        for b in other]),
            hovertext=[f"<b>{b['landmark']}</b><br>" + ("Due but left out: " + left[b["id"]]["reason"]
                                                         if b["id"] in left and left[b["id"]]["due"]
                                                         else "not in today's plan") for b in other],
            hoverinfo="text", name="Not in plan"))
    if highlight:
        b = next((x for x in bins if x["id"] == highlight), None)
        if b:
            fig.add_trace(go.Scattermap(lat=[b["lat"]], lon=[b["lon"]], mode="markers",
                                        marker=dict(size=40, color="rgba(220,38,38,0.25)"), hoverinfo="skip",
                                        showlegend=False))
    _places(fig, places)
    c = (np.mean([b["lat"] for b in bins]), np.mean([b["lon"] for b in bins]))
    return _layout(fig, c, 14.2, height, tiles)


def bins_map(bins: list[dict], status: dict[str, str], fills: dict[str, float], places: dict,
             beat_colour: dict[str, str] | None = None, net: roads.Network | None = None, tiles: str = "osm",
             height: int = 560, highlight: str | None = None) -> go.Figure:
    fig = _base(net, tiles)
    if beat_colour:
        fig.add_trace(go.Scattermap(lat=[b["lat"] for b in bins], lon=[b["lon"] for b in bins], mode="markers",
                                    marker=dict(size=22, color=[beat_colour.get(b["id"], NOT_PLANNED) for b in bins]),
                                    hoverinfo="skip", showlegend=False))
    for st_name in ("red", "orange", "amber", "green"):
        sel = [b for b in bins if status.get(b["id"]) == st_name]
        if not sel:
            continue
        fig.add_trace(go.Scattermap(
            lat=[b["lat"] for b in sel], lon=[b["lon"] for b in sel], mode="markers",
            marker=dict(size=13, color=STATUS_COLOURS[st_name]),
            hovertext=[f"<b>{b['landmark']}</b><br>{b['street']}<br>{_fill_words(fills.get(b['id'], 0))}"
                       for b in sel], hoverinfo="text", name=fmt.NOW_WORDS[st_name].capitalize()))
    if highlight:
        b = next((x for x in bins if x["id"] == highlight), None)
        if b:
            fig.add_trace(go.Scattermap(lat=[b["lat"]], lon=[b["lon"]], mode="markers",
                                        marker=dict(size=42, color="rgba(29,78,216,0.25)"), hoverinfo="skip",
                                        showlegend=False))
    _places(fig, places)
    c = (np.mean([b["lat"] for b in bins]), np.mean([b["lon"] for b in bins]))
    return _layout(fig, c, 14.2, height, tiles)
