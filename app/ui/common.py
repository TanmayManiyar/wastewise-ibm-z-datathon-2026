"""Shared helpers for the Streamlit pages: session, database, formatting, live refresh."""
from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wastewise import audit, auth, config, db, fmt, platform_info, sessions  # noqa: E402
from wastewise.fmt import (STATUS_COLOURS, duration, hhmm, km, km1, litres, when)  # noqa: E402,F401 - one formatting module

STATUS_WORDS = fmt.PLAN_WORDS
LEVEL_ICON = {"critical": ":material/error:", "warning": ":material/warning:", "info": ":material/info:"}


def conn():
    """One SQLite connection per browser session (connections are not shared between sessions)."""
    if "db" not in st.session_state:
        st.session_state.db = db.connect()
    return st.session_state.db


def user() -> dict | None:
    """The signed-in user. A reload or a dropped connection starts a new Streamlit session; the signed
    session cookie (wastewise.sessions) brings the user back without a new sign-in until it expires."""
    u = st.session_state.get("user")
    if u and u.get("session_exp") and time.time() >= u["session_exp"]:
        audit.log(conn(), u["username"], "logout", {"reason": "session expired"})
        st.session_state.pop("user", None)
        st.session_state["_clear_cookie"] = True
        return None
    if not u:
        try:
            token = st.context.cookies.get(sessions.COOKIE)
        except Exception:
            token = None
        u = sessions.check(conn(), token) if token else None
        if u:
            st.session_state.user = u
            st.session_state["_cookie_written"] = True      # the browser already has it
            audit.log(conn(), u["username"], "session_resumed", {"role": u["role"]})
    return u


def sign_in(u: dict) -> None:
    token, max_age = sessions.issue(u["username"], u["role"])
    st.session_state.user = u | {"session_exp": int(time.time()) + max_age}
    st.session_state["_cookie"] = (token, max_age)
    st.session_state.pop("_cookie_written", None)


def sign_out() -> None:
    u = st.session_state.get("user")
    if u:
        sessions.revoke(conn(), u["username"])
    keep = st.session_state.get("db")
    st.session_state.clear()
    if keep is not None:
        st.session_state.db = keep
    st.session_state["_clear_cookie"] = True


def cookie_sync() -> None:
    """Write or clear the session cookie in the browser (Streamlit itself cannot set cookies)."""
    js = None
    if st.session_state.pop("_clear_cookie", False):
        js = f"document.cookie = '{sessions.COOKIE}=; Max-Age=0; Path=/; SameSite=Strict';"
    elif st.session_state.get("_cookie") and not st.session_state.get("_cookie_written"):
        token, max_age = st.session_state["_cookie"]
        js = (f"document.cookie = '{sessions.COOKIE}={token}; Max-Age={int(max_age)}; Path=/; SameSite=Strict'"
              " + (location.protocol === 'https:' ? '; Secure' : '');")
        st.session_state["_cookie_written"] = True
    if js:
        st.html(f"<script>{js}</script>", unsafe_allow_javascript=True)


def role() -> str | None:
    u = st.session_state.get("user")
    return u["role"] if u else None


def can(permission: str) -> bool:
    return auth.can(role(), permission)


def log(action: str, detail: dict | None = None) -> None:
    u = st.session_state.get("user")
    audit.log(conn(), u["username"] if u else None, action, detail or {})


def today_label(d) -> str:
    return fmt.day_name(str(d))


@st.cache_data(ttl=3600, show_spinner=False)
def platform_badge() -> dict:
    return platform_info.detect()


@st.cache_data(ttl=600, show_spinner=False)
def engine_label() -> str | None:
    """What runs the forecaster's trees here, with the times measured on this machine."""
    from wastewise import forecast
    try:
        c = forecast._inference_model(db.connect())
    except Exception:
        return None
    if not c:
        return None
    eng = c.get("engine")
    return eng.summary() if eng else f"forecast trees on the CPU ({c['model'].gbt_lib})"


def header_badge() -> str:
    """'IBM LinuxONE s390x · Telum yes · <engine> · Granite' style badge."""
    p = platform_badge()
    parts = []
    if p["is_s390x"]:
        parts.append(f"IBM LinuxONE s390x ({p['machine_name'] or p['machine_type']})")
        parts.append("Telum " + ("yes" if p["telum"] else "no"))
    else:
        parts.append(f"{p['system']} {p['arch']} - not LinuxONE")
    eng = engine_label()
    if eng:
        parts.append(eng)
    llm = st.session_state.get("_llm_label")
    if llm:
        parts.append(llm)
    return " · ".join(parts)


def synthetic_notice() -> None:
    if db.get_meta(conn(), "synthetic_info"):
        st.caption(":material/science: Demonstration mode: sensor history, collections and complaints shown here are "
                   "**synthetic** (generated), except records entered by users or the API.")


def live_refresh(interval_s: int = 8) -> None:
    """Rerun the page whenever anyone changes data (readings, complaints, plans...)."""
    if "_seen_version" not in st.session_state:
        st.session_state._seen_version = db.data_version(conn())

    @st.fragment(run_every=interval_s)
    def _watch():
        v = db.data_version(conn())
        if v != st.session_state._seen_version:
            st.session_state._seen_version = v
            st.rerun(scope="app")

    _watch()


def mark_seen() -> None:
    st.session_state._seen_version = db.data_version(conn())


def css() -> None:
    st.markdown("""
<style>
  .ww-badge {display:inline-block;padding:2px 10px;border-radius:999px;font-size:0.8rem;font-weight:600;
             border:1px solid #CBD5E1;background:#F8FAFC;color:#0F172A;}
  .ww-draft {background:#FEF3C7;border-color:#F59E0B;color:#78350F;}
  .ww-approved {background:#DCFCE7;border-color:#16A34A;color:#14532D;}
  .ww-platform {font-size:0.78rem;color:#334155;}
  .ww-big {font-size:2.1rem;font-weight:700;line-height:1.15;overflow-wrap:anywhere;}
  .ww-mid {font-size:1.25rem;}
  div[data-testid="stMetricValue"] {font-size:1.5rem;}
  @media print {
    header, footer, [data-testid="stSidebar"], [data-testid="stToolbar"], .stButton, .no-print {display:none !important;}
  }
</style>""", unsafe_allow_html=True)


# ---------------------------------------------------------------- plain-language numbers
def change(new: float, old: float, unit: str, fewer: str = "less", more: str = "more", digits: int = 0) -> str:
    """'32 km less than the old routine' style text."""
    d = round(new - old, digits)
    if abs(d) < (10 ** -digits) / 2:
        return "same as the old routine"
    n = f"{abs(d):,.{digits}f}"
    return f"{n} {unit} {fewer if d < 0 else more} than the old routine"


def vehicle_names() -> dict[str, str]:
    from wastewise import db as _db
    return {v["id"]: v["name"] for v in _db.vehicles(conn(), active_only=False)}
