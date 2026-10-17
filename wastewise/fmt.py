"""One way to write numbers, times, dates and fill status - used by the web pages, the briefing and
crew messages, and the PDF documents, so the same figure looks the same everywhere.

  km(70.43)        -> "70 km"          summaries (whole kilometres)
  km1(4.26)        -> "4.3 km"         route sheets (one decimal)
  litres(12000)    -> "12,000 L"
  hhmm(ts)         -> "06:40"
  day_name(...)    -> "Sunday 18 October"
  when(ts)         -> "18 Oct 06:40"
  duration(150)    -> "2 h 30 min"
  fill(87.4)       -> "87%"  (over 105 % -> "overflowing")

Fill status, one colour scale everywhere (map dots, table pills and bars, legends):
  red     full now (Bins page) / already full when the shift starts (plans)
  orange  full by the time the truck arrives (plans) / full within 3 hours (Bins page)
  amber   over half full
  green   fine
"""
from __future__ import annotations

from datetime import date, datetime

from . import config

STATUS_COLOURS = {"red": "#DC2626", "orange": "#EA580C", "amber": "#FACC15", "green": "#16A34A"}
STATUS_EMOJI = {"red": "🔴", "orange": "🟠", "amber": "🟡", "green": "🟢"}
# words for plans (status at the truck's arrival) and for the Bins page (status now)
PLAN_WORDS = {"red": "already full at shift start", "orange": "full by the time the truck arrives",
              "amber": "over half full", "green": "fine"}
NOW_WORDS = {"red": "full now", "orange": "full within 3 hours", "amber": "over half full", "green": "fine"}
PLAN_PILL = {"red": "Already full", "orange": "Full by arrival", "amber": "Over half", "green": "Fine"}
NOW_PILL = {"red": "Full now", "orange": "Full within 3 h", "amber": "Over half", "green": "Fine"}
SOON_HOURS = 3


def status_now(fill_now: float, fill_in_3h: float | None = None) -> str:
    """Bins page: how full a bin is now, and whether it will be full within 3 hours."""
    if fill_now >= 100:
        return "red"
    if fill_in_3h is not None and fill_in_3h >= 100:
        return "orange"
    return "amber" if fill_now >= 50 else "green"


def status_plan(fill_at_start: float, fill_at_arrival: float) -> str:
    """Plans: full when the round starts, full by the truck's arrival, over half, fine."""
    if fill_at_start >= 100:
        return "red"
    if fill_at_arrival >= 100:
        return "orange"
    return "amber" if fill_at_arrival >= 50 else "green"


def colour_for_fill(pct: float) -> str:
    """Colour of a fill bar when no status is known (full = red, over half = amber, else green)."""
    return STATUS_COLOURS["red" if pct >= 100 else "amber" if pct >= 50 else "green"]


def legend(words: dict[str, str]) -> str:
    return " · ".join(f"{STATUS_EMOJI[k]} {words[k]}" for k in ("red", "orange", "amber", "green"))


# ------------------------------------------------------------------ numbers
def km(v: float | None) -> str:
    if v is None:
        return "-"
    return "less than 1 km" if 0 < v < 0.5 else f"{int(round(v)):,} km"


def km1(v: float | None) -> str:
    return "-" if v is None else f"{v:,.1f} km"


def litres(v: float | None) -> str:
    return "-" if v is None else f"{int(round(v)):,} L"


def fill(pct: float | None) -> str:
    if pct is None:
        return "-"
    return "overflowing" if pct > 105 else f"{min(pct, 100):.0f}%"


def duration(minutes: float) -> str:
    """150 -> '2 h 30 min', 40 -> '40 min'."""
    m = int(round(max(0.0, float(minutes))))
    if m < 60:
        return f"{m} min"
    h, r = divmod(m, 60)
    return f"{h} h" + (f" {r} min" if r else "")


# ------------------------------------------------------------------ times and dates
def _local(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, config.IST)


def hhmm(ts: float | None) -> str:
    return "-" if ts is None else _local(ts).strftime("%H:%M")


def when(ts: float | None) -> str:
    if ts is None:
        return "-"
    d = _local(ts)
    return f"{d.day} {d:%b} {d:%H:%M}"


def day_name(d: date | datetime | str | int | float) -> str:
    """'Sunday 18 October' from a date, 'YYYY-MM-DD' text or a timestamp."""
    if isinstance(d, str):
        d = datetime.strptime(d[:10], "%Y-%m-%d")
    elif isinstance(d, (int, float)):
        d = _local(d)
    return f"{d:%A} {d.day} {d:%B}"


def short_day(d: date | datetime | str) -> str:
    """'18 Oct 2026' for document headers."""
    if isinstance(d, str):
        d = datetime.strptime(d[:10], "%Y-%m-%d")
    return f"{d.day} {d:%b %Y}"
