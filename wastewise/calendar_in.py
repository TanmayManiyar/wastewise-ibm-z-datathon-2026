"""Festival and season calendar for Navi Mumbai used by the generator and forecaster.

Dates are approximate (lunar calendar festivals move every year) and should be
checked against the official NMMC / Maharashtra holiday list.
"""
from __future__ import annotations

from datetime import date, timedelta

# name, first day, last day, multiplier on waste per site type ("*" = all sites)
FESTIVALS = [
    ("Ganeshotsav", date(2025, 8, 27), date(2025, 9, 6),
     {"*": 1.25, "temple": 1.9, "gaothan": 1.6, "housing": 1.35, "ground": 1.5, "eatery": 1.4}),
    ("Navratri", date(2025, 9, 22), date(2025, 10, 2),
     {"*": 1.15, "temple": 1.7, "ground": 1.8, "housing": 1.2}),
    ("Diwali", date(2025, 10, 18), date(2025, 10, 23),
     {"*": 1.35, "housing": 1.6, "supermarket": 1.7, "eatery": 1.3, "gaothan": 1.5}),
    ("Holi", date(2026, 3, 3), date(2026, 3, 4), {"*": 1.15, "housing": 1.3, "ground": 1.4}),
    ("Gudi Padwa", date(2026, 3, 19), date(2026, 3, 19), {"*": 1.1, "housing": 1.25, "temple": 1.4}),
    ("Eid al-Adha", date(2026, 5, 27), date(2026, 5, 28), {"*": 1.1, "housing": 1.2, "gaothan": 1.3}),
    ("Ganeshotsav", date(2026, 9, 14), date(2026, 9, 24),
     {"*": 1.25, "temple": 1.9, "gaothan": 1.6, "housing": 1.35, "ground": 1.5, "eatery": 1.4}),
    ("Navratri", date(2026, 10, 11), date(2026, 10, 19),
     {"*": 1.15, "temple": 1.7, "ground": 1.8, "housing": 1.2}),
    ("Dussehra", date(2026, 10, 20), date(2026, 10, 20), {"*": 1.15, "temple": 1.5, "ground": 1.6}),
    ("Diwali", date(2026, 11, 6), date(2026, 11, 11),
     {"*": 1.35, "housing": 1.6, "supermarket": 1.7, "eatery": 1.3, "gaothan": 1.5}),
]

# South-west monsoon in Mumbai: roughly 10 June - 30 September.
MONSOON = ((6, 10), (9, 30))


def festival_on(d: date) -> tuple[str, dict] | None:
    for name, start, end, mult in FESTIVALS:
        if start <= d <= end:
            return name, mult
    return None


def festival_multiplier(d: date, site_type: str) -> float:
    f = festival_on(d)
    if f is None:
        return 1.0
    mult = f[1]
    return mult.get(site_type, mult.get("*", 1.0))


def is_monsoon(d: date) -> bool:
    (m1, d1), (m2, d2) = MONSOON
    return date(d.year, m1, d1) <= d <= date(d.year, m2, d2)


def days_to_next_festival(d: date, horizon: int = 30) -> int:
    for k in range(horizon + 1):
        if festival_on(d + timedelta(days=k)):
            return k
    return horizon + 1
