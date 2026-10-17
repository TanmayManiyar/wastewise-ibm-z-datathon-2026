"""WasteWise web app (Streamlit). Run: streamlit run app/main.py"""
from __future__ import annotations

import sys
from pathlib import Path

APP = Path(__file__).resolve().parent
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP.parent))

import streamlit as st  # noqa: E402

st.set_page_config(page_title="WasteWise · Sanpada", page_icon=":material/recycling:", layout="wide",
                   initial_sidebar_state="auto")

from ui.common import conn, css, header_badge, log, user  # noqa: E402
from views import bins, crew, help as help_view, login, reports, settings, today, whatif  # noqa: E402
from wastewise import auth  # noqa: E402

css()
u = user()
if not u:
    login.page()
    st.stop()

if u["role"] == "crew":
    pages = [st.Page(crew.page, title="My route", icon=":material/local_shipping:", url_path="my-route")]
else:
    pages = [
        st.Page(today.page, title="Today's plan", icon=":material/route:", url_path="today", default=True),
        st.Page(bins.page, title="Bins", icon=":material/delete:", url_path="bins"),
        st.Page(whatif.page, title="What-if planning", icon=":material/alt_route:", url_path="what-if"),
        st.Page(reports.page, title="Reports", icon=":material/bar_chart:", url_path="reports"),
    ]
    if auth.can(u["role"], "settings") or auth.can(u["role"], "edit_bins"):
        pages.append(st.Page(settings.page, title="Settings", icon=":material/settings:", url_path="settings"))
    pages.append(st.Page(help_view.page, title="Help", icon=":material/help:", url_path="help"))

nav = st.navigation(pages)


@st.cache_data(ttl=60, show_spinner=False)
def _llm_ok(_v: int) -> str | None:
    from wastewise import db, llm
    s = llm.status(db.connect())
    return "Granite" if s["ok"] else None


st.session_state._llm_label = _llm_ok(0)
with st.sidebar:
    st.markdown("### WasteWise")
    st.caption("Sanpada ward office · NMMC")
    st.markdown(f"**{u['display_name']}**  \n{auth.ROLES[u['role']]}")
    if st.button("Sign out", icon=":material/logout:"):
        log("logout")
        st.session_state.clear()
        st.rerun()
    st.divider()
    st.markdown(f"<span class='ww-platform'>{header_badge()}</span>", unsafe_allow_html=True)
nav.run()
