"""Help: who does what, in plain steps."""
import streamlit as st

from ui.common import STATUS_COLOURS


def page() -> None:
    st.title("Help")
    st.write("WasteWise tells the Sanpada ward office every day which bins to collect, which truck goes where and "
             "in what order on the real streets - and shows afterwards what the crews actually did.")
    a, b = st.columns(2)
    with a:
        with st.container(border=True):
            st.markdown("""
#### Control-room operator
**Before the shift (by 05:45)**
1. Open **Today's plan** and press **Generate plan**.
2. Read the coloured boxes - each one says *what to do*.
3. Ask the supervisor to approve.

**During the shift**
1. Watch **Live alerts** - it refreshes by itself.
2. A citizen calls? **Bins > Report a problem**: type their words (any language), press **Understand**, choose the bin, **Confirm**.
3. Something big changed (complaints, a truck delayed)? **Re-plan from now**, then the supervisor approves - the crews' phones update.
4. A crew phones in? Record it under **Crew phone view**.
""")
        with st.container(border=True):
            st.markdown("""
#### Ward supervisor
1. Check the draft plan: map, alerts, route tables.
2. Press **Approve plan** - only approved plans reach the crews.
3. Print the **route sheets** for crews without a phone.
4. After the shift: **Progress** - read the exceptions, enter the returned paper sheets, download the CSV.
5. Weekly: **Reports > Bin sizing** - bins that are too small or over-serviced.
""")
    with b:
        with st.container(border=True):
            st.markdown("""
#### Collection crew (phone)
1. Sign in - you only see **My route**.
2. The big text is your **next stop**. Tap **Open in Google Maps** to drive there.
3. Tap **how full** it was, then **Collected** - or **Could not reach** / **Bin missing**.
4. Red **Truck full** banner: go to the Turbhe landfill, unload, tap **Unloaded**.
5. Choose English, मराठी or हिंदी at the top.
""")
        with st.container(border=True):
            st.markdown("""
#### Administrator
- **Settings**: shift times, trucks and depot, users and passwords, API keys, the AI engine.
- **Refresh demo data** (demonstration mode only), demo feed and test events under **Live alerts**.
- **Check audit log integrity** - every action is recorded in a tamper-evident log.

#### Viewer
Can look at plans, bins and reports; cannot change anything.
""")
    st.markdown("#### What the colours mean")
    words = {"red": "full already at the start of the shift", "orange": "full by the time the truck arrives",
             "amber": "over half full", "green": "fine"}
    st.markdown(" &nbsp; ".join(
        f"<span style='display:inline-block;width:14px;height:14px;border-radius:7px;background:{STATUS_COLOURS[k]};"
        f"vertical-align:middle'></span> {v}" for k, v in words.items()), unsafe_allow_html=True)
    st.markdown("Truck colours are blue, purple and teal - the line and the ring around each stop. Black dot: a due "
                "bin that did not fit (send an extra trip). Grey: not in today's plan.")
    st.markdown("#### Good to know")
    st.markdown("""
- **Due bins**: a bin is collected every N days (usually every day). A due bin is left out only if no truck has space
  or time; then an alert asks for an extra trip.
- **Landfill trips**: when the next bin would not fit, the truck goes to the Turbhe landfill, unloads (about 20 min)
  and continues. Every route ends with an unload before the depot.
- **Evening round**: after the morning plan, WasteWise checks which bins will fill up again and overflow before
  tomorrow's round, and suggests the best time for a second pickup (Today's plan > Evening round). You can also
  plan a round at any time and for any bins you choose. It is approved like the morning plan and appears on the
  crews' phones when their morning route is finished.
- **Areas (beats)**: each truck has its own part of the ward; it helps elsewhere only when that clearly saves time.
- **Demonstration data**: in demo mode the sensor history is generated (synthetic) and labelled as such.
- **Depot and landfill positions** are approximate and must be confirmed with NMMC (Settings > Vehicles & depot).
""")
