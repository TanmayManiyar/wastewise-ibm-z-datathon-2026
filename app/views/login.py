"""Sign-in page.

Default: one-click sign-in - choose who you are (administrator, supervisor, operator, viewer or a truck's
crew) and you are in; no password. Sign out to switch to another user type. Every sign-in is still recorded
in the audit log. The administrator can switch passwords back on (Settings > Users > "Sign-in").
"""
import streamlit as st

from wastewise import audit, auth, db

from ui.common import conn, header_badge, sign_in

ICONS = {"admin": ":material/admin_panel_settings:", "supervisor": ":material/supervisor_account:",
         "operator": ":material/support_agent:", "viewer": ":material/visibility:", "crew": ":material/local_shipping:"}
ORDER = ["admin", "supervisor", "operator", "viewer", "crew"]
WHAT = {"admin": "everything, including Settings", "supervisor": "approve plans, bins, complaints",
        "operator": "make plans, complaints, live alerts", "viewer": "look only",
        "crew": "the truck's route on a phone"}


def passwordless(c) -> bool:
    return bool(db.get_settings(c).get("passwordless_signin", True))


def page() -> None:
    c = conn()
    left, mid, right = st.columns([1, 1.3, 1])
    with mid:
        st.title("WasteWise")
        st.write("Daily bin collection for the **Sanpada ward office** (NMMC).")
        st.markdown(f"<span class='ww-platform'>{header_badge()}</span>", unsafe_allow_html=True)
        if passwordless(c):
            one_click(c)
        else:
            password_form(c)
        st.caption("You stay signed in on this device for one shift (crews 12 hours, office 8 hours), even if you "
                   "reload the page or the phone locks. Press **Sign out** to switch to another user.")


def one_click(c) -> None:
    st.markdown("#### Who are you?")
    vehicles = {v["id"]: v["name"] for v in db.vehicles(c, active_only=False)}
    users = [u for u in auth.users(c) if u["active"]]
    users.sort(key=lambda u: (ORDER.index(u["role"]) if u["role"] in ORDER else 9, u["vehicle_id"] or "", u["username"]))
    for u in users:
        label = (f"Crew - {vehicles.get(u['vehicle_id'], u['vehicle_id'])}" if u["role"] == "crew"
                 else auth.ROLES[u["role"]])
        if st.button(f"{label}  ·  {WHAT.get(u['role'], '')}", key=f"as_{u['username']}", icon=ICONS.get(u["role"]),
                     use_container_width=True, type="primary" if u["role"] == "admin" else "secondary"):
            row = db.one(c, "SELECT username, display_name, role, vehicle_id FROM users WHERE username=?",
                         (u["username"],))
            audit.log(c, row["username"], "login", {"role": row["role"], "method": "one-click (no password)"})
            sign_in(row)
            st.rerun()


def password_form(c) -> None:
    with st.form("login"):
        u = st.text_input("User name", autocomplete="username")
        p = st.text_input("Password", type="password", autocomplete="current-password")
        ok = st.form_submit_button("Sign in", type="primary", use_container_width=True)
    if ok:
        user = auth.authenticate(c, u, p)
        if user:
            sign_in(user)
            st.rerun()
        else:
            st.error("Sign-in failed. Check the user name and password. After 5 wrong tries the account "
                     "is locked for 5 minutes.")
    st.caption("New installation? The administrator finds the first passwords in "
               "`secrets/initial_passwords.txt` on the server.")
