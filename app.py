"""
app.py — Streamlit entry point.

Run with: streamlit run app.py
"""

import streamlit as st
from src.db import get_conn
from pathlib import Path

st.set_page_config(
    page_title="Wheel Trader",
    page_icon="⚙",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Initialize DB on first load
@st.cache_resource
def init_db():
    with get_conn() as conn:
        pass  # schema applied in get_conn on first connect
    return True

init_db()

# Start fill confirmation poller (idempotent on every rerun)
from src.poller import start_poller
start_poller()

# Navigation
pages = [
    st.Page("app_home.py", title="Home", icon="🏠", default=True),
    st.Page("pages/1_Dashboard.py", title="Dashboard", icon="📊"),
    st.Page("pages/2_Positions.py", title="Positions", icon="📌"),
    st.Page("pages/3_Ledger.py", title="Ledger", icon="📒"),
    st.Page("pages/4_Eligibility.py", title="Eligibility", icon="✅"),
]
pg = st.navigation(pages)
pg.run()
