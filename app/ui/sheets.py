"""Printable route sheets (one per truck), including landfill rows."""
from __future__ import annotations

import html

from .common import hhmm, today_label

CSS = """
<style>
 body{font-family:Arial,Helvetica,sans-serif;margin:18px;color:#000}
 h1{font-size:20px;margin:0 0 4px} .sub{font-size:13px;margin-bottom:10px}
 .demo{border:2px solid #000;padding:4px 8px;font-weight:bold;margin:6px 0;display:inline-block}
 table{border-collapse:collapse;width:100%;font-size:12.5px}
 th,td{border:1px solid #000;padding:5px 6px;vertical-align:top}
 th{background:#E5E7EB;text-align:left}
 tr.lf td{background:#F3F4F6;font-weight:bold}
 tr.dep td{background:#F9FAFB}
 .box{display:inline-block;width:11px;height:11px;border:1px solid #000;margin-right:3px;vertical-align:middle}
 .sig{margin-top:22px;display:flex;gap:40px;font-size:13px}
 .sig div{flex:1;border-top:1px solid #000;padding-top:4px}
 @media print{body{margin:6mm} .noprint{display:none}}
</style>"""


def route_sheet_html(plan: dict, route: dict, crew_name: str | None = None) -> str:
    e = html.escape
    rows = []
    n = 0
    for s in route["stops"]:
        if s["kind"] == "bin":
            n += 1
            rows.append(
                f"<tr><td>{n}</td><td>{hhmm(s['arrive_ts'])}</td><td><b>{e(s['bin_id'])}</b> {e(s['landmark'])}</td>"
                f"<td>{e(s['street'] or '')}</td><td>{s['expected_fill_pct']:.0f}%</td>"
                f"<td><span class=box></span>Collected<br><span class=box></span>Could not reach<br>"
                f"<span class=box></span>Bin missing</td>"
                f"<td><span class=box></span>&frac14; <span class=box></span>&frac12; <span class=box></span>&frac34; "
                f"<span class=box></span>Full <span class=box></span>Overflowing</td><td></td></tr>")
        elif s["kind"] == "landfill":
            rows.append(f"<tr class=lf><td>&#9654;</td><td>{hhmm(s['arrive_ts'])}</td>"
                        f"<td colspan=4>LANDFILL - Turbhe: unload (about "
                        f"{(s['depart_ts'] - s['arrive_ts']) / 60:.0f} min). Truck carries about "
                        f"{s['load_before_l']:,.0f} L.</td><td>Left at:</td><td></td></tr>")
        elif s["kind"] == "depot":
            rows.append(f"<tr class=dep><td>&#9632;</td><td>{hhmm(s['arrive_ts'])}</td>"
                        f"<td colspan=4>Back at the depot</td><td></td><td></td></tr>")
    demo = ("<div class=demo>DEMONSTRATION - computed from synthetic data</div>" if plan.get("label") else "")
    status = plan.get("status", "draft").upper()
    t = route["totals"]
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Route sheet {e(route['vehicle_name'])}</title>
{CSS}</head><body>
<h1>WasteWise route sheet - {e(route['vehicle_name'])}</h1>
<div class=sub>{today_label(plan['plan_date'])} · start {hhmm(route['start_ts'])} from {e(route['start_stop'].title())}
 · back by {hhmm(route['shift_end_ts'])} · plan #{plan.get('id', '-')} ({status})
 · {t['bins']} bins, {t['landfill_trips']} landfill trip(s), {t['distance_km']} km
 · crew: {e(crew_name or '________________')}</div>
{demo}
<table><tr><th>#</th><th>Time</th><th>Bin / place</th><th>Street</th><th>Expected</th><th>What happened</th>
<th>How full was it?</th><th>Time done</th></tr>
{''.join(rows)}</table>
<div class=sig><div>Driver signature</div><div>Supervisor signature</div><div>Notes</div></div>
<p class=noprint style="font-size:12px">Print this page (Ctrl+P). Return the filled sheet to the ward office;
it is entered under Today's plan > Progress.</p>
</body></html>"""
