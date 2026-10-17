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

from wastewise import audit, auth, config, db, platform_info  # noqa: E402

STATUS_COLOURS = {"red": "#DC2626", "orange": "#EA580C", "amber": "#FACC15", "green": "#16A34A"}
STATUS_WORDS = {"red": "full at shift start", "orange": "full by arrival", "amber": "over half full",
                "green": "fine"}
LEVEL_ICON = {"critical": ":material/error:", "warning": ":material/warning:", "info": ":material/info:"}
IDLE_LOGOUT_S = 30 * 60


def conn():
    """One SQLite connection per browser session (connections are not shared between sessions)."""
    if "db" not in st.session_state:
        st.session_state.db = db.connect()
    return st.session_state.db


def user() -> dict | None:
    u = st.session_state.get("user")
    if u and time.time() - st.session_state.get("last_seen", 0) > IDLE_LOGOUT_S:
        audit.log(conn(), u["username"], "logout", {"reason": "idle timeout"})
        st.session_state.pop("user", None)
        return None
    st.session_state.last_seen = time.time()
    return u


def role() -> str | None:
    u = st.session_state.get("user")
    return u["role"] if u else None


def can(permission: str) -> bool:
    return auth.can(role(), permission)


def log(action: str, detail: dict | None = None) -> None:
    u = st.session_state.get("user")
    audit.log(conn(), u["username"] if u else None, action, detail or {})


def hhmm(ts: float | None) -> str:
    return "-" if ts is None else datetime.fromtimestamp(ts, config.IST).strftime("%H:%M")


def when(ts: float | None) -> str:
    return "-" if ts is None else datetime.fromtimestamp(ts, config.IST).strftime("%d %b %H:%M")


def today_label(d) -> str:
    return datetime.strptime(str(d), "%Y-%m-%d").strftime("%A %d %B %Y")


@st.cache_data(ttl=3600, show_spinner=False)
def platform_badge() -> dict:
    return platform_info.detect()


def header_badge() -> str:
    """'IBM LinuxONE s390x · Telum yes · <engine> · Granite' style badge."""
    p = platform_badge()
    parts = []
    if p["is_s390x"]:
        parts.append(f"IBM LinuxONE s390x ({p['machine_name'] or p['machine_type']})")
        parts.append("Telum " + ("yes" if p["telum"] else "no"))
    else:
        parts.append(f"{p['system']} {p['arch']} - not LinuxONE")
    eng = st.session_state.get("_engine_label")
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
