"""
Streamlit dashboard for human review queue and dispute metrics.

Run: streamlit run frontend/dashboard.py
"""

from __future__ import annotations

import os
from datetime import datetime

import httpx
import streamlit as st

API_BASE = os.getenv("API_BASE_URL", "http://localhost:8000")

st.set_page_config(
    page_title="Fraud Guardian - Review Dashboard",
    page_icon="🛡️",
    layout="wide",
)


def fetch_api(path: str, method: str = "GET", **kwargs) -> dict | list | None:
    """Fetch data from the API."""
    try:
        with httpx.Client(base_url=API_BASE, timeout=10) as client:
            if method == "GET":
                resp = client.get(path, params=kwargs.get("params"))
            else:
                resp = client.post(path, json=kwargs.get("json"))
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        st.error(f"API Error: {e}")
        return None


def render_metrics():
    """Display key performance metrics."""
    st.header("📊 Dispute Metrics")
    data = fetch_api("/api/v1/disputes/metrics/summary")
    if data is None:
        st.warning("Could not load metrics. Is the API running?")
        return

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Total Disputes", data.get("total_disputes", 0))
    with col2:
        st.metric("Automation Rate", f"{data.get('automation_rate_pct', 0):.1f}%",
                   delta="Target: ≥85%")
    with col3:
        st.metric("Win Rate", f"{data.get('win_rate_pct', 0):.1f}%",
                   delta="Target: ≥70%")
    with col4:
        st.metric("Auto-Submitted", data.get("auto_submitted", 0))


def render_review_queue():
    """Display disputes pending human review."""
    st.header("📋 Human Review Queue")

    data = fetch_api("/api/v1/disputes/", params={"status": "pending_review", "limit": 50})
    if data is None:
        return

    disputes = data.get("disputes", [])
    if not disputes:
        st.success("No disputes pending review!")
        return

    st.info(f"{len(disputes)} dispute(s) awaiting review")

    for dispute in disputes:
        with st.expander(
            f"🔍 {dispute['id']} | ${dispute['amount_cents']/100:.2f} | {dispute.get('merchant_id', 'N/A')}",
            expanded=False,
        ):
            # Dispute details
            detail = fetch_api(f"/api/v1/disputes/{dispute['id']}")
            if detail is None:
                continue

            col1, col2 = st.columns(2)
            with col1:
                st.write("**Dispute ID:**", detail["id"])
                st.write("**Transaction ID:**", detail["transaction_id"])
                st.write("**Merchant:**", detail["merchant_id"])
                st.write("**Amount:**", f"${detail['amount_cents']/100:.2f}")
                st.write("**Reason Code:**", detail["reason_code"])

            with col2:
                fraud_score = detail.get("fraud_score")
                if fraud_score is not None:
                    color = "🟢" if fraud_score < 0.3 else "🟡" if fraud_score < 0.7 else "🔴"
                    st.write(f"**Fraud Score:** {color} {fraud_score:.4f}")
                st.write("**Evidence Strength:**", detail.get("evidence_strength", "N/A"))
                st.write("**Decision Rationale:**", detail.get("decision_rationale", "N/A"))
                if detail.get("deadline"):
                    st.write("**Deadline:**", detail["deadline"])

            # Review actions
            st.divider()
            reviewer_id = st.text_input("Reviewer ID", key=f"rev_{dispute['id']}", value="reviewer_001")
            notes = st.text_area("Notes", key=f"notes_{dispute['id']}", height=68)

            col_approve, col_reject = st.columns(2)
            with col_approve:
                if st.button("✅ Approve & Submit", key=f"approve_{dispute['id']}", type="primary"):
                    result = fetch_api(
                        f"/api/v1/disputes/{dispute['id']}/review",
                        method="POST",
                        json={"decision": "approve", "reviewer_id": reviewer_id, "notes": notes},
                    )
                    if result:
                        st.success(f"Dispute approved: {result.get('message', '')}")
                        st.rerun()

            with col_reject:
                if st.button("❌ Reject", key=f"reject_{dispute['id']}"):
                    result = fetch_api(
                        f"/api/v1/disputes/{dispute['id']}/review",
                        method="POST",
                        json={"decision": "reject", "reviewer_id": reviewer_id, "notes": notes},
                    )
                    if result:
                        st.warning(f"Dispute rejected: {result.get('message', '')}")
                        st.rerun()


def render_recent_disputes():
    """Display recent dispute activity."""
    st.header("📜 Recent Disputes")
    data = fetch_api("/api/v1/disputes/", params={"limit": 20})
    if data is None:
        return

    disputes = data.get("disputes", [])
    if not disputes:
        st.info("No disputes yet.")
        return

    # Status color coding
    status_colors = {
        "received": "🔵",
        "scoring": "🟡",
        "evidence_collection": "🟡",
        "pending_review": "🟠",
        "auto_submitted": "🟢",
        "manually_submitted": "🟢",
        "won": "✅",
        "lost": "❌",
        "expired": "⚪",
    }

    for d in disputes:
        icon = status_colors.get(d.get("status", ""), "⚪")
        st.write(
            f"{icon} **{d['id'][:20]}...** | "
            f"${d['amount_cents']/100:.2f} | "
            f"{d.get('status', 'unknown')} | "
            f"{d.get('decision', '-')} | "
            f"{d.get('created_at', '')[:19]}"
        )


def main():
    st.title("🛡️ Toast Fraud Guardian")
    st.caption("Real-time fraud detection & autonomous chargeback dispute system")

    tab_metrics, tab_review, tab_recent = st.tabs(["Metrics", "Review Queue", "Recent"])

    with tab_metrics:
        render_metrics()

    with tab_review:
        render_review_queue()

    with tab_recent:
        render_recent_disputes()

    # Auto-refresh
    st.sidebar.header("Settings")
    auto_refresh = st.sidebar.checkbox("Auto-refresh (30s)", value=False)
    if auto_refresh:
        import time
        time.sleep(30)
        st.rerun()

    st.sidebar.divider()
    st.sidebar.caption("Toast Fraud Guardian v0.1.0")
    health = fetch_api("/health")
    if health:
        st.sidebar.success(f"API: {health['status']} | Model: {health['model_version']}")
    else:
        st.sidebar.error("API unreachable")


if __name__ == "__main__":
    main()
