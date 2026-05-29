"""
Earlybird — Slack Alert Client
Sends formatted, actionable incident alerts to Slack.
Uses Block Kit for rich formatting.
"""

import requests
import logging
from datetime import datetime
from typing import Optional
from app.config import settings

logger = logging.getLogger(__name__)

SEVERITY_EMOJI = {
    "critical": "🔴",
    "high": "🟠",
    "medium": "🟡",
    "low": "🔵",
    "observe": "⚪",
}

SEVERITY_COLOR = {
    "critical": "#FF0000",
    "high": "#FF6600",
    "medium": "#FFAA00",
    "low": "#0099FF",
    "observe": "#AAAAAA",
}


def send_incident_alert(
    incident_id: str,
    fingerprint: str,
    severity: str,
    score: int,
    affected_users: int,
    event_count: int,
    countries: list,
    endpoint: str,
    service: str,
    agent_alert_timestamp: datetime,
    llm_summary: Optional[dict] = None,
    suggested_owner: Optional[str] = None,
) -> Optional[str]:
    """
    Send a rich Slack alert for an incident.
    Returns the Slack message timestamp (used as message ID).
    """
    if not settings.SLACK_WEBHOOK_URL:
        logger.warning("[SLACK] No webhook URL configured, skipping alert")
        return None

    emoji = SEVERITY_EMOJI.get(severity, "❓")
    color = SEVERITY_COLOR.get(severity, "#AAAAAA")
    title = llm_summary.get("title", f"Anomaly on {endpoint}") if llm_summary else f"Anomaly on {endpoint}"
    summary = llm_summary.get("summary", "No AI summary available.") if llm_summary else ""
    root_cause = llm_summary.get("suspected_root_cause", "") if llm_summary else ""
    next_steps = llm_summary.get("recommended_next_steps", []) if llm_summary else []
    support_msg = llm_summary.get("support_message", "") if llm_summary else ""

    countries_str = ", ".join(countries) if countries else "Unknown"
    steps_text = "\n".join(f"  {i+1}. {step}" for i, step in enumerate(next_steps[:5]))

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{emoji} EARLYBIRD — {severity.upper()} INCIDENT",
                "emoji": True
            }
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{title}*"
            }
        },
        {"type": "divider"},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Service:*\n{service}"},
                {"type": "mrkdwn", "text": f"*Endpoint:*\n`{endpoint}`"},
                {"type": "mrkdwn", "text": f"*Impact:*\n{affected_users} users, {event_count} errors"},
                {"type": "mrkdwn", "text": f"*Region:*\n{countries_str}"},
                {"type": "mrkdwn", "text": f"*Score:*\n{score} / severity: {severity}"},
                {"type": "mrkdwn", "text": f"*Owner:*\n{suggested_owner or 'Unknown'}"},
            ]
        },
    ]

    if summary:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Guardian Analysis:*\n{summary}"
            }
        })

    if root_cause:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Suspected Root Cause:*\n{root_cause}"
            }
        })

    if steps_text:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Suggested Next Steps:*\n{steps_text}"
            }
        })

    if support_msg:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Support Message:*\n_{support_msg}_"
            }
        })

    blocks.extend([
        {"type": "divider"},
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"⏱️ *Agent Alert Timestamp:* `{agent_alert_timestamp.isoformat()}`  |  "
                        f"🆔 Incident: `{str(incident_id)[:8]}`  |  "
                        f"🔍 Fingerprint: `{fingerprint}`  |  "
                        f"📋 Freshdesk: _Monitoring..._"
                    )
                }
            ]
        }
    ])

    payload = {
        "channel": settings.SLACK_ALERT_CHANNEL,
        "attachments": [
            {
                "color": color,
                "blocks": blocks,
            }
        ]
    }

    try:
        response = requests.post(
            settings.SLACK_WEBHOOK_URL,
            json=payload,
            timeout=5,
        )
        response.raise_for_status()
        logger.info(f"[SLACK] Alert sent for incident {incident_id}")
        return response.headers.get("X-Slack-Req-Timestamp")
    except requests.RequestException as e:
        logger.error(f"[SLACK] Failed to send alert: {e}")
        return None
