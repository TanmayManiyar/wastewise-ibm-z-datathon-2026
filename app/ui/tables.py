"""Readable HTML tables.

Streamlit's st.dataframe sends data as binary Apache Arrow in the server's byte order, which a
little-endian browser misreads when the server is IBM LinuxONE (big-endian). HTML text has no
byte order, and lets us show plain-language columns: coloured status pills, fill bars and
Google Maps links instead of raw numbers and coordinates.
"""
from __future__ import annotations

import html
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable

import streamlit as st

PILL = {"red": ("#FEE2E2", "#991B1B"), "orange": ("#FFEDD5", "#9A3412"), "amber": ("#FEF9C3", "#854D0E"),
        "green": ("#DCFCE7", "#166534"), "blue": ("#DBEAFE", "#1E40AF"), "grey": ("#F1F5F9", "#334155")}

CSS = """
<style>
.ww-table-wrap{overflow-x:auto;border:1px solid #E2E8F0;border-radius:8px;margin:4px 0 12px 0}
table.ww-table{border-collapse:collapse;width:100%;font-size:0.92rem}
table.ww-table th{background:#F8FAFC;text-align:left;padding:7px 10px;font-weight:600;color:#334155;
  border-bottom:1px solid #E2E8F0;white-space:nowrap}
table.ww-table td{padding:6px 10px;border-bottom:1px solid #F1F5F9;vertical-align:middle}
table.ww-table tr.ww-strong td{background:#F8FAFC;font-weight:600}
table.ww-table td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.ww-pill{display:inline-block;padding:1px 9px;border-radius:999px;font-size:0.82rem;font-weight:600;white-space:nowrap}
.ww-bar{position:relative;width:120px;height:14px;background:#F1F5F9;border-radius:7px;overflow:hidden;
  display:inline-block;vertical-align:middle;margin-right:6px}
.ww-bar span{position:absolute;left:0;top:0;bottom:0;border-radius:7px}
.ww-maplink{text-decoration:none;white-space:nowrap}
</style>"""


@dataclass
class Col:
    key: str
    label: str
    kind: str = "text"          # text | num | pill | bar | map
    fmt: Callable[[Any], str] | None = None


def maps_url(lat: float, lon: float) -> str:
    return "https://www.google.com/maps/search/?" + urllib.parse.urlencode({"api": 1, "query": f"{lat:.6f},{lon:.6f}"})


def pill(text: str, colour: str = "grey") -> str:
    bg, fg = PILL.get(colour, PILL["grey"])
    return f"<span class='ww-pill' style='background:{bg};color:{fg}'>{html.escape(str(text))}</span>"


def bar(pct: float | None, label: str | None = None) -> str:
    if pct is None:
        return "-"
    p = max(0.0, float(pct))
    colour = "#DC2626" if p >= 100 else "#F59E0B" if p >= 50 else "#16A34A"
    width = min(p, 100.0)
    txt = html.escape(label if label is not None else ("overflowing" if p > 105 else f"{p:.0f}%"))
    return f"<span class='ww-bar'><span style='width:{width:.0f}%;background:{colour}'></span></span>{txt}"


def _cell(row: dict, c: Col) -> tuple[str, str]:
    v = row.get(c.key)
    if c.kind == "map":
        if not v:
            return "", ""
        lat, lon = v
        return f"<a class='ww-maplink' href='{maps_url(lat, lon)}' target='_blank' rel='noopener'>📍 Map</a>", ""
    if c.kind == "bar":
        return bar(v), ""
    if c.kind == "pill":
        if not v:
            return "", ""
        text, colour = v if isinstance(v, tuple) else (v, "grey")
        return pill(text, colour), ""
    if v is None or v == "":
        return "", "num" if c.kind == "num" else ""
    s = c.fmt(v) if c.fmt else str(v)
    return html.escape(s), "num" if c.kind == "num" else ""


def table(rows: list[dict], cols: list[Col], strong: Callable[[dict], bool] | None = None,
          max_height: int | None = None) -> None:
    """Render rows (dicts) as a clean HTML table."""
    head = "".join(f"<th>{html.escape(c.label)}</th>" for c in cols)
    body = []
    for r in rows:
        cells = []
        for c in cols:
            content, cls = _cell(r, c)
            cells.append(f"<td class='{cls}'>{content}</td>")
        cls = " class='ww-strong'" if strong and strong(r) else ""
        body.append(f"<tr{cls}>{''.join(cells)}</tr>")
    style = f" style='max-height:{max_height}px;overflow-y:auto'" if max_height else ""
    st.markdown(CSS + f"<div class='ww-table-wrap'{style}><table class='ww-table'><thead><tr>{head}</tr></thead>"
                f"<tbody>{''.join(body)}</tbody></table></div>", unsafe_allow_html=True)


def csv_of(rows: list[dict], cols: list[Col]) -> str:
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([c.label for c in cols if c.kind != "map"])
    for r in rows:
        out = []
        for c in cols:
            if c.kind == "map":
                continue
            v = r.get(c.key)
            if isinstance(v, tuple):
                v = v[0]
            out.append("" if v is None else (c.fmt(v) if c.fmt else v))
        w.writerow(out)
    return buf.getvalue()
