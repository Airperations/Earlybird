"""
Earlybird — Streamlit Dashboard
Visual demo for judges. Shows the live scoreboard.
Run with: streamlit run dashboard.py
"""

import os
import streamlit as st
import requests
import pandas as pd
from datetime import datetime
import time

API_BASE = os.getenv("API_BASE", "http://localhost:8000")

# Sent on dashboard API calls so it works whether or not the API enforces a key.
DASHBOARD_HEADERS = {}
if os.getenv("DASHBOARD_API_KEY"):
    DASHBOARD_HEADERS["x-dashboard-key"] = os.getenv("DASHBOARD_API_KEY")

st.set_page_config(
    page_title="Earlybird — Bounty Dashboard",
    page_icon="🛡️",
    layout="wide",
)

# ─── Header ──────────────────────────────────────────────────────────────────

st.title("🛡️ Earlybird")
st.caption("Bounty 2 — Early Incident Detection vs Freshdesk Support")
st.divider()

# ─── Auto-refresh ────────────────────────────────────────────────────────────

auto_refresh = st.sidebar.checkbox("Auto-refresh (30s)", value=True)
if auto_refresh:
    st.sidebar.caption("Dashboard refreshes every 30 seconds")

# ─── Fetch data ──────────────────────────────────────────────────────────────

@st.cache_data(ttl=30)
def fetch_summary():
    try:
        r = requests.get(f"{API_BASE}/dashboard/summary", headers=DASHBOARD_HEADERS, timeout=5)
        return r.json()
    except Exception as e:
        return {"error": str(e)}


@st.cache_data(ttl=30)
def fetch_incidents():
    try:
        r = requests.get(f"{API_BASE}/dashboard/incidents", headers=DASHBOARD_HEADERS, timeout=5)
        return r.json()
    except Exception as e:
        return {"incidents": [], "error": str(e)}


summary = fetch_summary()
incidents_data = fetch_incidents()

if "error" in summary:
    st.error(f"⚠️ Cannot connect to API: {summary['error']}")
    st.info("Make sure the Earlybird API is running: `uvicorn app.main:app --reload`")
    st.stop()

# ─── Bounty Metric Banner ─────────────────────────────────────────────────────

bounty = summary.get("bounty_metric", {})
win_rate = bounty.get("win_rate_percent", 0)
passing = bounty.get("passing", False)

if passing:
    st.success(f"✅ WIN RATE: {win_rate}% — PASSING the 80% bounty bar!")
else:
    st.error(f"❌ WIN RATE: {win_rate}% — Below 80% target")

# ─── KPI Columns ─────────────────────────────────────────────────────────────

col1, col2, col3, col4, col5 = st.columns(5)

race = summary.get("race_results", {})
incidents_meta = summary.get("incidents", {})
lead = summary.get("lead_time", {})

col1.metric("🏆 Win Rate", f"{win_rate}%", help="Agent won / Total matched")
col2.metric("🏆 Agent Won", race.get("agent_won", 0))
col3.metric("❌ Agent Lost", race.get("agent_lost", 0))
col4.metric("⚡ Avg Lead Time", lead.get("average_human", "—"), help="How many seconds before Freshdesk")
col5.metric("🛡️ Prevented", incidents_meta.get("prevented_no_ticket", 0), help="Incidents detected with no ticket ever created")

st.divider()

# ─── Incidents Table ──────────────────────────────────────────────────────────

st.subheader("📋 Incident Race Log")
st.caption("Agent Alert Timestamp vs First Freshdesk Ticket — The Bounty Evidence Table")

incidents = incidents_data.get("incidents", [])

if not incidents:
    st.info("No incidents yet. Send a test webhook to start the demo.")
else:
    df = pd.DataFrame(incidents)

    # Reorder and rename for display
    display_cols = {
        "incident_id": "ID",
        "title": "Incident",
        "severity": "Severity",
        "affected_users": "Users",
        "agent_alert_timestamp": "Agent Alert ⏱️",
        "freshdesk_ticket_timestamp": "Freshdesk Ticket ⏱️",
        "lead_time_human": "Lead Time",
        "outcome_emoji": "Result",
        "confidence": "Match Confidence",
    }

    display_df = df[[c for c in display_cols.keys() if c in df.columns]].rename(columns=display_cols)

    # Color rows by outcome
    def highlight_outcome(row):
        if "WON" in str(row.get("Result", "")):
            return ["background-color: #1a3a1a"] * len(row)
        elif "LOST" in str(row.get("Result", "")):
            return ["background-color: #3a1a1a"] * len(row)
        return [""] * len(row)

    st.dataframe(
        display_df,
        use_container_width=True,
        hide_index=True,
    )

# ─── Trigger Demo ────────────────────────────────────────────────────────────

st.divider()
st.subheader("🧪 Demo Controls")

col_a, col_b, col_c = st.columns(3)

with col_a:
    if st.button("🚨 Simulate Sentry Error (Critical)", use_container_width=True):
        payload = {
            "project_slug": "payments-api",
            "event": {
                "title": "GatewayTimeout: Payment provider did not respond",
                "message": "GatewayTimeout: Payment provider did not respond",
                "platform": "python",
                "release": "v1.42.0",
                "tags": [["environment", "production"], ["country_code", "MX"], ["http.status_code", "502"]],
                "request": {"url": "https://api.airdrive.com/api/v1/withdraw/confirm"},
                "exception": {
                    "values": [{"type": "GatewayTimeout", "value": "Payment provider did not respond"}]
                },
                "user": {"id": "user_demo_001"},
                "contexts": {"response": {"status_code": 502}},
            }
        }
        try:
            r = requests.post(f"{API_BASE}/webhooks/sentry/", json=payload, timeout=5)
            st.success(f"✅ Webhook sent! Response: {r.json()}")
            st.cache_data.clear()
        except Exception as e:
            st.error(f"Error: {e}")

with col_b:
    if st.button("🔄 Trigger Freshdesk Sync", use_container_width=True):
        try:
            r = requests.post(f"{API_BASE}/freshdesk/sync", timeout=5)
            st.success("✅ Sync triggered!")
            st.cache_data.clear()
        except Exception as e:
            st.error(f"Error: {e}")

with col_c:
    if st.button("🔁 Refresh Dashboard", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

# ─── Footer ──────────────────────────────────────────────────────────────────

st.divider()
st.caption(
    f"Earlybird | "
    f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')} | "
    f"Bounty 2 — Airdrive"
)

# Auto-refresh
if auto_refresh:
    time.sleep(30)
    st.rerun()
