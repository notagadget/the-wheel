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

st.title("Wheel Trader")
st.caption("Navigate using the sidebar.")
