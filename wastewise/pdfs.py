"""Printable PDF documents: collection route sheets and the end-of-shift report.

Built with fpdf2 (pure Python, LGPL) so it runs unchanged on IBM LinuxONE. The built-in PDF fonts
cover Latin text only, so the few landmark names written in Devanagari are printed transliterated.
"""
from __future__ import annotations

import re
import urllib.parse
from datetime import datetime

from fpdf import FPDF

from . import config, fmt

BLUE = (29, 78, 216)
GREY = (100, 116, 139)
HEAD_BG = (241, 245, 249)
LANDFILL_BG = (219, 234, 254)
LINE = (203, 213, 225)


# Readable English spellings for landmark names that OpenStreetMap only has in Devanagari.
KNOWN_NAMES = {"मनमोहन रेस्टॉरंट": "Manmohan Restaurant"}


def _latin(text) -> str:
    """Make text printable with the built-in PDF fonts (Devanagari -> Latin letters)."""
    if text is None:
        return ""
    s = str(text)
    for dev, eng in KNOWN_NAMES.items():
        s = s.replace(dev, eng)
    if re.search(r"[ऀ-ॿ]", s):
        from .complaints import transliterate
        s = " ".join(transliterate(w).capitalize() if re.search(r"[ऀ-ॿ]", w) else w for w in s.split())
    s = s.replace("—", "-").replace("–", "-").replace("’", "'").replace("→", "->")
    return s.encode("latin-1", "replace").decode("latin-1")


def _hm(ts) -> str:
    return fmt.hhmm(ts) if ts else ""


def _date(d: str) -> str:
    return fmt.short_day(d)


def _maps(lat, lon) -> str:
    return "https://www.google.com/maps/search/?" + urllib.parse.urlencode({"api": 1, "query": f"{lat:.6f},{lon:.6f}"})


class _Doc(FPDF):
    def __init__(self, footer_note: str):
        super().__init__(orientation="L", unit="mm", format="A4")
        self.footer_note = footer_note
        self.set_auto_page_break(False)
        self.set_margins(12, 12, 12)

    def footer(self):
        self.set_y(-10)
        self.set_font("Helvetica", "", 7.5)
        self.set_text_color(*GREY)
        self.cell(0, 5, _latin(self.footer_note), align="L")
        self.set_x(-40)
        self.cell(28, 5, f"Page {self.page_no()}/{{nb}}", align="R")


def _header(pdf: _Doc, kicker: str, title: str, info: list[tuple[str, str]], demo: bool) -> None:
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(*GREY)
    pdf.cell(0, 5, _latin(kicker.upper()), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "B", 19)
    pdf.set_text_color(15, 23, 42)
    pdf.cell(0, 10, _latin(title), new_x="LMARGIN", new_y="NEXT")
    pdf.set_draw_color(*BLUE)
    pdf.set_line_width(0.7)
    pdf.line(12, pdf.get_y() + 1, pdf.w - 12, pdf.get_y() + 1)
    pdf.ln(4)
    pdf.set_text_color(15, 23, 42)
    # each item takes the width it needs; the spare width is shared out as gaps
    widths = []
    for label, value in info:
        pdf.set_font("Helvetica", "B", 9.5)
        lw = pdf.get_string_width(_latin(label)) + 1.5
        pdf.set_font("Helvetica", "", 9.5)
        widths.append((lw, pdf.get_string_width(_latin(value)) + 1))
    gap = max(4.0, (pdf.w - 24 - sum(a + b for a, b in widths)) / max(len(info) - 1, 1))
    for (label, value), (lw, vw) in zip(info, widths):
        pdf.set_font("Helvetica", "B", 9.5)
        pdf.cell(lw, 6, _latin(label))
        pdf.set_font("Helvetica", "", 9.5)
        pdf.cell(vw + gap, 6, _latin(value))
    pdf.ln(8)
    if demo:
        pdf.set_font("Helvetica", "B", 8.5)
        pdf.set_text_color(146, 64, 14)
        pdf.cell(0, 5, "DEMONSTRATION - computed from synthetic data", new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(15, 23, 42)
        pdf.ln(1)


def _table_head(pdf: _Doc, cols: list[tuple[str, float]]) -> None:
    pdf.set_font("Helvetica", "B", 8.5)
    pdf.set_fill_color(*HEAD_BG)
    pdf.set_draw_color(*LINE)
    pdf.set_line_width(0.2)
    pdf.set_text_color(51, 65, 85)
    for name, w in cols:
        pdf.cell(w, 8, _latin(name), border=1, fill=True)
    pdf.ln()
    pdf.set_text_color(15, 23, 42)


ROUTE_COLS = [("STOP", 13), ("ARRIVAL", 18), ("BIN", 19), ("LANDMARK", 64), ("STREET", 58), ("EST. FILL", 22),
              ("MAP", 13), ("DONE", 17), ("FILL WHEN EMPTIED", 49)]


def _short(names: str | None, limit: int = 48) -> str:
    """Crew names for the sheet header (long lists are shortened so the header fits on one line)."""
    if not names:
        return ""
    return names if len(names) <= limit else names[:limit - 3].rsplit(",", 1)[0] + ", ..."


def _fill_text(p: float) -> str:
    return fmt.fill(p).capitalize()


def _fit(pdf: FPDF, text: str, w: float) -> str:
    text = _latin(text)
    while text and pdf.get_string_width(text) > w - 2:
        text = text[:-2] + "."
    return text


def route_sheets(plan: dict, routes: list[dict], crew: dict[str, str] | None = None,
                 depot_name: str = "Depot") -> bytes:
    """One page (or more) per truck, in the layout ward offices use for paper route sheets."""
    crew = crew or {}
    pdf = _Doc(f"WasteWise - plan for {plan['plan_date']} ({plan.get('status', 'draft')}) - printed "
               f"{fmt.when(datetime.now(config.IST).timestamp())}")
    pdf.alias_nb_pages()
    depot = depot_name.split(" (")[0].replace("vehicle depot", "depot")
    for r in routes:
        if not r["stops"]:
            continue
        t = r["totals"]
        title = ("Evening collection route sheet" if plan.get("kind") == "extra" else "Collection route sheet")
        info = [("Date", _date(plan["plan_date"])), ("Departure", f"{_hm(r['start_ts'])} from {depot}"),
                ("Planned return", _hm(t["end_ts"])), ("Stops", str(t["bins"])),
                ("Distance", fmt.km1(t["distance_km"])), ("Crew", _short(crew.get(r["vehicle_id"])) or "____________")]

        def new_page():
            pdf.add_page()
            _header(pdf, "Solid waste management - Sanpada", f"{title} - {r['vehicle_name']}", info,
                    bool(plan.get("label")))
            _table_head(pdf, ROUTE_COLS)

        new_page()
        n = 0
        for s in r["stops"]:
            if pdf.get_y() > pdf.h - 24:
                new_page()
            pdf.set_font("Helvetica", "", 9)
            if s["kind"] == "bin":
                n += 1
                vals = [str(n), _hm(s["arrive_ts"]), s["bin_id"], s["landmark"], s["street"] or "",
                        _fill_text(s["expected_fill_pct"])]
                for (name, w), v in zip(ROUTE_COLS[:6], vals):
                    pdf.cell(w, 8, _fit(pdf, v, w), border=1)
                pdf.set_text_color(*BLUE)
                pdf.set_font("Helvetica", "U", 9)
                pdf.cell(ROUTE_COLS[6][1], 8, "Map", border=1, link=_maps(s["lat"], s["lon"]))
                pdf.set_font("Helvetica", "", 9)
                pdf.set_text_color(15, 23, 42)
                pdf.cell(ROUTE_COLS[7][1], 8, "", border=1)
                pdf.cell(ROUTE_COLS[8][1], 8, "", border=1)
                pdf.ln()
            elif s["kind"] == "landfill":
                pdf.set_fill_color(*LANDFILL_BG)
                pdf.set_font("Helvetica", "B", 8.5)
                pdf.cell(ROUTE_COLS[0][1], 7, "", border=1, fill=True)
                pdf.cell(ROUTE_COLS[1][1], 7, _hm(s["arrive_ts"]), border=1, fill=True)
                span = sum(w for _, w in ROUTE_COLS[2:7])
                mins = (s["depart_ts"] - s["arrive_ts"]) / 60
                pdf.cell(span, 7, _latin(f"Turbhe landfill - unload (about {mins:.0f} min), truck carries about "
                                         f"{fmt.litres(s['load_before_l'])}"), border=1, fill=True)
                pdf.cell(ROUTE_COLS[7][1] + ROUTE_COLS[8][1], 7, "Left at:", border=1, fill=True)
                pdf.ln()
            else:
                pdf.set_font("Helvetica", "B", 8.5)
                pdf.cell(ROUTE_COLS[0][1], 7, "", border=1)
                pdf.cell(ROUTE_COLS[1][1], 7, _hm(s["arrive_ts"]), border=1)
                pdf.cell(sum(w for _, w in ROUTE_COLS[2:]), 7, _latin(f"Back at {depot}"), border=1)
                pdf.ln()
        pdf.ln(8)
        if pdf.get_y() > pdf.h - 30:
            pdf.add_page()
        pdf.set_font("Helvetica", "", 9)
        for label in ("Driver signature", "Supervisor signature", "Notes"):
            pdf.cell(85, 6, _latin(label + "  ______________________"))
        pdf.ln()
    return bytes(pdf.output())


def shift_report(day: str, rows: list[dict], summary: dict[str, dict], labels: dict[str, str],
                 demo: bool = False) -> bytes:
    """End-of-shift report: per-truck totals, exceptions with what to do, every stop."""
    pdf = _Doc(f"WasteWise end-of-shift report - {day} - printed {fmt.when(datetime.now(config.IST).timestamp())}")
    pdf.alias_nb_pages()
    pdf.add_page()
    tot = {k: sum(s[k] for s in summary.values()) for k in ("planned", "collected", "late", "problems", "missed")}
    _header(pdf, "Solid waste management - Sanpada", "End-of-shift report",
            [("Date", _date(day)), ("Planned stops", str(tot["planned"])), ("Collected", str(tot["collected"])),
             ("Late", str(tot["late"])), ("Problems", str(tot["problems"])), ("Missed", str(tot["missed"]))], demo)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 7, "By truck", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9.5)
    for veh, s in summary.items():
        pdf.cell(0, 6, _latin(f"{veh}: {s['collected']} of {s['planned']} collected, {s['late']} late, "
                              f"{s['problems']} problem(s), {s['missed']} missed"), new_x="LMARGIN", new_y="NEXT")
    ex = [r for r in rows if r["status"] in ("late", "could_not_reach", "bin_missing", "overdue", "missed")]
    if ex:
        pdf.ln(2)
        pdf.set_font("Helvetica", "B", 11)
        pdf.cell(0, 7, "Exceptions and what to do", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 9)
        for r in ex:
            if pdf.get_y() > pdf.h - 24:
                pdf.add_page()
            pdf.multi_cell(0, 5, _latin(f"- {r['landmark']} ({r['vehicle_name']}, planned {_hm(r['planned_ts'])}): "
                                        f"{labels.get(r['status'], r['status'])}. {r['action']}"),
                           new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)
    cols = [("TRUCK", 30), ("PLACE", 68), ("PLANNED", 18), ("DONE AT", 18), ("STATUS", 32), ("HOW FULL", 20),
            ("PROOF OF SERVICE", 48), ("RECORDED BY", 39)]
    _table_head(pdf, cols)
    pdf.set_font("Helvetica", "", 9)
    for r in rows:
        if pdf.get_y() > pdf.h - 22:
            pdf.add_page()
            _table_head(pdf, cols)
            pdf.set_font("Helvetica", "", 9)
        vals = [r["vehicle_name"], r["landmark"], _hm(r["planned_ts"]), _hm(r["actual_ts"]),
                labels.get(r["status"], r["status"]),
                f"{r['fill_reported']:.0f}%" if r["fill_reported"] is not None else "", r.get("proof_text") or "",
                r["by"] or ""]
        for (n, w), v in zip(cols, vals):
            pdf.cell(w, 7, _fit(pdf, v, w), border=1)
        pdf.ln()
    return bytes(pdf.output())


def monthly_report(m: dict) -> bytes:
    """Monthly summary for NMMC headquarters: totals, complaints, crews' performance, fuel and maintenance."""
    pdf = _Doc(f"WasteWise monthly summary - {m['label']} - printed {fmt.when(datetime.now(config.IST).timestamp())}")
    pdf.alias_nb_pages()
    pdf.add_page()
    t = m["totals"]
    _header(pdf, "Solid waste management - Sanpada ward (NMMC)", f"Monthly summary - {m['label']}",
            [("Collections", f"{t['collections']:,}"), ("Waste collected", fmt.litres(t["litres"])),
             ("Could not reach", str(t["not_reached"])), ("Bins missing", str(t["missing"])),
             ("Complaints", f"{t['complaints']} ({t['complaints_closed']} closed)")], m["synthetic"])

    def heading(text):
        pdf.ln(2)
        pdf.set_font("Helvetica", "B", 11)
        pdf.cell(0, 7, _latin(text), new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 9.5)

    heading("Complaints")
    pdf.cell(0, 6, _latin(f"{t['complaints']} received, {t['complaints_closed']} closed by staff"
                          + (f"; median time to close {m['median_close_h']} h" if m["median_close_h"] is not None else "")
                          + "."), new_x="LMARGIN", new_y="NEXT")
    heading("Crews (all visits this month)")
    cols = [("TRUCK", 40), ("VISITS", 22), ("ON TIME", 24), ("MISSED", 24), ("PROBLEMS", 24), ("GPS VERIFIED", 30)]
    _table_head(pdf, cols)
    pdf.set_font("Helvetica", "", 9)
    for r in m["performance"]:
        vals = [r["truck"], str(r["visits"]), "-" if r["on_time_pct"] is None else f"{r['on_time_pct']}%",
                "-" if r["missed_pct"] is None else f"{r['missed_pct']}%", str(r["problems"]),
                "-" if r["verified_pct"] is None else f"{r['verified_pct']}%"]
        for (_, w), v in zip(cols, vals):
            pdf.cell(w, 7, _fit(pdf, v, w), border=1)
        pdf.ln()
    heading("Fuel and maintenance (truck log)")
    if not m["fuel"]:
        pdf.cell(0, 6, "No entries in the truck log this month.", new_x="LMARGIN", new_y="NEXT")
    for vid, f in m["fuel"].items():
        pdf.cell(0, 6, _latin(f"{vid}: {fmt.litres(f['fuel_l'])} diesel, Rs {f['cost_rs']:,.0f}, "
                              f"{f['maintenance']} maintenance, {f['breakdowns']} breakdown(s)"),
                 new_x="LMARGIN", new_y="NEXT")
    heading("Day by day")
    cols = [("DATE", 30), ("COLLECTIONS", 28), ("LITRES", 30), ("NOT REACHED", 28), ("MISSING", 22),
            ("COMPLAINTS", 26), ("CLOSED", 22), ("KM (PLANS)", 26)]
    _table_head(pdf, cols)
    pdf.set_font("Helvetica", "", 9)
    for r in m["days"]:
        if pdf.get_y() > pdf.h - 22:
            pdf.add_page()
            _table_head(pdf, cols)
            pdf.set_font("Helvetica", "", 9)
        vals = [_date(r["date"]), str(r["collections"]), fmt.litres(r["litres"]), str(r["not_reached"]),
                str(r["missing"]), str(r["complaints"]), str(r["complaints_closed"]),
                fmt.km(r["km_planned"]) if r["km_planned"] else "-"]
        for (_, w), v in zip(cols, vals):
            pdf.cell(w, 7, _fit(pdf, v, w), border=1)
        pdf.ln()
    return bytes(pdf.output())
