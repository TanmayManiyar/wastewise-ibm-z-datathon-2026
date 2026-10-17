"""Local IBM Granite language model: pre-shift briefing, driver messages, complaint help.

The model runs in a llama.cpp server on the same machine (OpenAI-compatible
/v1/chat/completions). Only localhost addresses are accepted, so no data
leaves the server. Granite only rewrites facts taken from the plan:
  * every number and time in its text is checked against the plan's facts;
    if anything does not match, the text is rejected and the built-in
    template (always correct) is shown instead, with the reason;
  * for complaints it only suggests place words; a person still confirms the bin.
Without a model, the templates (English / Hindi / Marathi) are used.
"""
from __future__ import annotations

import json
import re
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime
from urllib.parse import urlparse

from . import config, db

DEFAULT_URL = "http://127.0.0.1:8080"
LANG_NAMES = {"en": "English", "hi": "Hindi (Devanagari script)", "mr": "Marathi (Devanagari script)"}
DEV_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")


def is_local(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "::1") or host.endswith(".localhost")


def _settings(conn) -> tuple[str, bool]:
    s = db.get_settings(conn)
    return s.get("llm_url", DEFAULT_URL), bool(s.get("llm_enabled", True))


def _http(url: str, payload: dict | None = None, timeout: float = 60) -> dict:
    if not is_local(url):
        raise RuntimeError("language model must run on this machine")
    req = urllib.request.Request(url, data=json.dumps(payload).encode() if payload is not None else None,
                                 headers={"Content-Type": "application/json"},
                                 method="POST" if payload is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def status(conn: sqlite3.Connection) -> dict:
    url, enabled = _settings(conn)
    out = {"url": url, "enabled": enabled, "ok": False, "model": None, "error": None}
    if not enabled:
        out["error"] = "switched off in Settings"
        return out
    try:
        d = _http(url.rstrip("/") + "/v1/models", timeout=2)
        out["model"] = (d.get("data") or [{}])[0].get("id")
        out["ok"] = True
    except Exception as exc:
        out["error"] = type(exc).__name__ if not isinstance(exc, urllib.error.URLError) else str(exc.reason)
    return out


def chat(conn: sqlite3.Connection, system: str, user: str, max_tokens: int = 350, temperature: float = 0.2) -> str:
    url, enabled = _settings(conn)
    if not enabled:
        raise RuntimeError("language model switched off")
    d = _http(url.rstrip("/") + "/v1/chat/completions",
              {"messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
               "max_tokens": max_tokens, "temperature": temperature,
               # stop at the end of the answer (base models tend to continue with a new chat turn)
               "stop": ["<|im_start|>", "<|im_end|>", "<|end_of_text|>", "\nuser", "\nUser:"]}, timeout=300)
    return d["choices"][0]["message"]["content"].strip()


# ------------------------------------------------------------------ facts and checking
def _hm(ts) -> str:
    return datetime.fromtimestamp(ts, config.IST).strftime("%H:%M")


def plan_facts(plan: dict) -> dict:
    routes = []
    for r in plan["routes"]:
        bins = [s for s in r["stops"] if s["kind"] == "bin"]
        if not bins:
            continue
        urgent = sorted((s for s in bins if s["status"] in ("red", "orange")), key=lambda s: s["arrive_ts"])
        routes.append({"truck": r["vehicle_name"], "vehicle_id": r["vehicle_id"], "bins": len(bins),
                       "start": _hm(r["start_ts"]), "back_by": _hm(r["totals"]["end_ts"]),
                       "landfill_trips": r["totals"]["landfill_trips"], "km": r["totals"]["distance_km"],
                       "first_stop": f"{bins[0]['bin_id']} {bins[0]['landmark']}",
                       "first_stop_time": _hm(bins[0]["arrive_ts"]),
                       "urgent_stops": [f"{s['bin_id']} {s['landmark']} at {_hm(s['arrive_ts'])}" for s in urgent[:3]]})
    k = plan["kpis"]
    return {"date": plan["plan_date"], "plan_id": plan.get("id"), "status": plan.get("status", "draft"),
            "trucks": len(routes), "bins_total": k["bins_served"], "due_left_out": k["due_left_out"],
            "distance_km": k["distance_km"], "landfill_trips": k["landfill_trips"],
            "last_truck_back": _hm(k["finish_ts"]), "routes": routes,
            "alerts": [a["text"] for a in plan.get("alerts", []) if a["level"] in ("critical", "warning")][:4]}


def _numbers(text: str) -> list[str]:
    t = text.translate(DEV_DIGITS)
    t = re.sub(r"\bSNP-\d{3}\b", " ", t)              # bin numbers are checked as names
    return re.findall(r"\d+(?:[.:]\d+)?", t.replace(",", ""))


def _norm(n: str) -> str:
    if ":" in n:
        h, m = n.split(":")
        return f"{int(h):02d}:{m}"
    try:
        f = float(n)
        return str(int(f)) if f == int(f) else f"{f:.2f}".rstrip("0").rstrip(".")
    except ValueError:
        return n


def allowed_numbers(facts: dict) -> set[str]:
    return {_norm(n) for n in _numbers(json.dumps(facts, ensure_ascii=False))}


def check(text: str, facts: dict) -> dict:
    allowed = allowed_numbers(facts)
    bad = sorted({n for n in _numbers(text) if _norm(n) not in allowed})
    ids = set(re.findall(r"SNP-\d{3}", json.dumps(facts)))
    bad_ids = sorted(set(re.findall(r"SNP-\d{3}", text)) - ids)
    return {"ok": not bad and not bad_ids, "unknown_numbers": bad, "unknown_bins": bad_ids,
            "numbers_checked": len(_numbers(text))}


# ------------------------------------------------------------------ templates (always correct)
def template_briefing(f: dict) -> str:
    lines = [f"Pre-shift briefing for {f['date']}: {f['trucks']} trucks will collect {f['bins_total']} bins "
             f"({f['distance_km']} km, {f['landfill_trips']} landfill trips). The last truck should be back by "
             f"{f['last_truck_back']}."]
    for r in f["routes"]:
        s = (f"{r['truck']}: {r['bins']} bins from {r['start']}, first stop {r['first_stop']} at "
             f"{r['first_stop_time']}, {r['landfill_trips']} landfill trip(s), back by {r['back_by']}.")
        if r["urgent_stops"]:
            s += " Already full or filling fast: " + "; ".join(r["urgent_stops"]) + "."
        lines.append(s)
    if f["due_left_out"]:
        lines.append(f"{f['due_left_out']} due bins do not fit today - arrange an extra trip.")
    for a in f["alerts"]:
        lines.append("Note: " + a)
    return "\n".join(lines)


DRIVER = {
    "en": ("Good morning {truck} crew. Today: {bins} bins, start {start} from the depot. First stop {first} at "
           "{first_time}. Landfill trips: {lf}. Please be back at the depot by {back}.{urgent}"),
    "hi": ("नमस्ते {truck} टीम। आज {bins} डिब्बे, डिपो से शुरुआत {start} बजे। पहला स्टॉप {first}, {first_time} बजे। "
           "लैंडफिल के चक्कर: {lf}। कृपया {back} बजे तक डिपो लौटें।{urgent}"),
    "mr": ("नमस्कार {truck} टीम. आज {bins} डबे, डेपोतून सुरुवात {start} वाजता. पहिला थांबा {first}, {first_time} वाजता. "
           "डंपिंग ग्राउंडच्या फेऱ्या: {lf}. कृपया {back} वाजेपर्यंत डेपोत परत या.{urgent}"),
}
URGENT_WORD = {"en": " Go first to: ", "hi": " पहले यहाँ जाएँ: ", "mr": " आधी इथे जा: "}


def template_driver(r: dict, lang: str) -> str:
    urgent = (URGENT_WORD[lang] + "; ".join(r["urgent_stops"]) + ".") if r["urgent_stops"] else ""
    return DRIVER[lang].format(truck=r["truck"], bins=r["bins"], start=r["start"], first=r["first_stop"],
                               first_time=r["first_stop_time"], lf=r["landfill_trips"], back=r["back_by"],
                               urgent=urgent)


# ------------------------------------------------------------------ generation with checking
SYSTEM = ("You write short, plain messages for the waste-collection staff of the Sanpada ward office (NMMC, "
          "Navi Mumbai). Use ONLY the facts in the JSON you are given. Do not invent any number, time, bin, "
          "place or name. Copy times exactly as written (HH:MM). No greetings beyond one short line.")


def _generate(conn, prompt: str, facts: dict, fallback: str, use_llm: bool) -> dict:
    if not use_llm:
        return {"text": fallback, "source": "template", "check": None}
    try:
        text = chat(conn, SYSTEM, prompt)
    except Exception as exc:
        return {"text": fallback, "source": "template", "check": None, "error": f"language model unavailable ({exc})"}
    chk = check(text, facts)
    if not chk["ok"]:
        return {"text": fallback, "source": "template", "check": chk, "rejected": text,
                "error": "Granite's text contained figures not in the plan - showing the checked template instead"}
    return {"text": text, "source": "Granite", "check": chk}


def briefing(conn: sqlite3.Connection, plan: dict, use_llm: bool = True) -> dict:
    f = plan_facts(plan)
    prompt = ("Write the pre-shift briefing (at most 120 words, English) for the ward supervisor from these "
              "facts:\n" + json.dumps(f, ensure_ascii=False))
    return _generate(conn, prompt, f, template_briefing(f), use_llm)


def driver_message(conn: sqlite3.Connection, plan: dict, vehicle_id: str, lang: str = "en",
                   use_llm: bool = True) -> dict:
    f = plan_facts(plan)
    r = next(x for x in f["routes"] if x["vehicle_id"] == vehicle_id)
    prompt = (f"Write a short message (at most 60 words) in {LANG_NAMES[lang]} for the crew of {r['truck']} from "
              "these facts:\n" + json.dumps(r, ensure_ascii=False))
    return _generate(conn, prompt, r, template_driver(r, lang), use_llm)


def complaint_place_words(conn: sqlite3.Connection, text: str) -> list[str]:
    """Ask Granite which words of a complaint name a place (in English spelling). Suggestion only."""
    out = chat(conn, "Extract place names from a citizen complaint about a garbage bin in Sanpada, Navi Mumbai. "
                     "Answer only with a JSON list of short English place names, e.g. [\"D-Mart Juinagar\"].",
               text, max_tokens=80, temperature=0.0)
    m = re.search(r"\[.*\]", out, re.S)
    if not m:
        return []
    try:
        words = json.loads(m.group(0))
    except ValueError:
        return []
    return [str(w)[:60] for w in words if isinstance(w, str)][:5]
