import streamlit as st

from wastewise import auth

from ui.common import conn, header_badge


def page() -> None:
    left, mid, right = st.columns([1, 1.3, 1])
    with mid:
        st.title("WasteWise")
        st.write("Daily bin collection for the **Sanpada ward office** (NMMC).")
        st.markdown(f"<span class='ww-platform'>{header_badge()}</span>", unsafe_allow_html=True)
        with st.form("login"):
            u = st.text_input("User name", autocomplete="username")
            p = st.text_input("Password", type="password", autocomplete="current-password")
            ok = st.form_submit_button("Sign in", type="primary", use_container_width=True)
        if ok:
            user = auth.authenticate(conn(), u, p)
            if user:
                st.session_state.user = user
                st.rerun()
            else:
                st.error("Sign-in failed. Check the user name and password. After 5 wrong tries the account "
                         "is locked for 5 minutes.")
        st.caption("New installation? The administrator finds the first passwords in "
                   "`secrets/initial_passwords.txt` on the server.")
