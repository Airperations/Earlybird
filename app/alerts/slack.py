"""
Earlybird — Slack Alert Client

Two-phase, fast-path alerting:

  1. send_immediate_alert(...)  → a MINIMAL alert sent the instant an incident
     crosses threshold. NO LLM call is made first. This is the message whose
     delivery locks the official benchmark timestamp.
  2. send_enrichment_followup(...) → the LLM summary, posted AFTER delivery as a
     thread reply (when a bot token is configured) or as a follow-up message.

Delivery is retried with exponential backoff (SLACK_MAX_RETRIES /
SLACK_RETRY_BACKOFF_SECONDS) and the result reports whether Slack actually
accepted the message, so a failed delivery is never mistaken for a real alert.

Transport:
  • SLACK_BOT_TOKEN set → chat.postMessage (returns a message `ts`, enabling
    real threaded enrichment replies).
  • otherwise → incoming webhook (works with zero OAuth setup; enrichment is
    posted as a standalone follow-up because webhooks don't return a ts).
"""

import time
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, List

import requests

from app.config import settings

logger = logging.getLogger(__name__)

SLACK_API_POST_MESSAGE = "https://slack.com/api/chat.postMessage"

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


@dataclass
class AlertDeliveryResult:
    """Outcome of a Slack delivery attempt."""
    delivered: bool
    message_id: Optional[str] = None   # chat.postMessage ts, or "ok" sentinel for webhooks
    thread_ts: Optional[str] = None    # parent ts for thread replies (bot token only)
    channel: Optional[str] = None
    attempts: int = 0
    error: Optional[str] = None


def _iso(value) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


# ─── Low-level transports (each returns AlertDeliveryResult for ONE attempt) ──

def _post_via_api(blocks: list, text: str, color: str,
                  thread_ts: Optional[str] = None) -> AlertDeliveryResult:
    """Post via chat.postMessage. Returns the message ts so replies can thread."""
    payload = {
        "channel": settings.SLACK_ALERT_CHANNEL,
        "text": text,  # fallback/notification text
        "attachments": [{"color": color, "blocks": blocks}],
    }
    if thread_ts:
        payload["thread_ts"] = thread_ts

    resp = requests.post(
        SLACK_API_POST_MESSAGE,
        json=payload,
        headers={"Authorization": f"Bearer {settings.SLACK_BOT_TOKEN}"},
        timeout=settings.SLACK_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    body = resp.json()
    if not body.get("ok"):
        return AlertDeliveryResult(delivered=False, error=body.get("error", "slack_api_error"))
    return AlertDeliveryResult(
        delivered=True,
        message_id=body.get("ts"),
        thread_ts=body.get("ts"),
        channel=body.get("channel"),
    )


def _post_via_webhook(blocks: list, text: str, color: str) -> AlertDeliveryResult:
    """Post via an incoming webhook. No ts is returned (no native threading)."""
    payload = {
        "channel": settings.SLACK_ALERT_CHANNEL,
        "text": text,
        "attachments": [{"color": color, "blocks": blocks}],
    }
    resp = requests.post(
        settings.SLACK_WEBHOOK_URL,
        json=payload,
        timeout=settings.SLACK_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    # Incoming webhooks return the literal body "ok" with no message ts.
    return AlertDeliveryResult(delivered=True, message_id="ok")


def _deliver_with_retry(blocks: list, text: str, color: str,
                        thread_ts: Optional[str] = None) -> AlertDeliveryResult:
    """
    Attempt delivery up to SLACK_MAX_RETRIES times with exponential backoff.
    Returns the first success, or a failed result carrying the last error and the
    number of attempts made.
    """
    if not settings.SLACK_BOT_TOKEN and not settings.SLACK_WEBHOOK_URL:
        logger.warning("[SLACK] No bot token or webhook URL configured — cannot deliver alert")
        return AlertDeliveryResult(delivered=False, attempts=0, error="not_configured")

    last_error: Optional[str] = None
    max_attempts = max(1, settings.SLACK_MAX_RETRIES)

    for attempt in range(1, max_attempts + 1):
        try:
            if settings.SLACK_BOT_TOKEN:
                result = _post_via_api(blocks, text, color, thread_ts=thread_ts)
            else:
                result = _post_via_webhook(blocks, text, color)
            result.attempts = attempt
            if result.delivered:
                return result
            last_error = result.error
        except requests.RequestException as e:
            last_error = str(e)
            logger.warning(f"[SLACK] Delivery attempt {attempt}/{max_attempts} failed: {e}")

        if attempt < max_attempts:
            time.sleep(settings.SLACK_RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1)))

    logger.error(f"[SLACK] Delivery failed after {max_attempts} attempts: {last_error}")
    return AlertDeliveryResult(delivered=False, attempts=max_attempts, error=last_error)


# ─── Phase 1: minimal immediate alert (NO LLM) ────────────────────────────────

def send_immediate_alert(
    incident_id: str,
    fingerprint: str,
    severity: str,
    score: int,
    affected_users: int,
    event_count: int,
    countries: list,
    endpoint: Optional[str],
    service: Optional[str],
    agent_alert_timestamp: Optional[datetime] = None,
    first_seen_at=None,
    last_seen_at=None,
    action: Optional[str] = None,
    platform: Optional[str] = None,
    provider: Optional[str] = None,
    suggested_owner: Optional[str] = None,
    status: str = "enriching…",
) -> AlertDeliveryResult:
    """
    Send the fast, minimal alert. Contains everything a responder needs to act
    NOW — no AI summary, so nothing blocks the official timestamp. The LLM
    analysis arrives later via send_enrichment_followup().
    """
    emoji = SEVERITY_EMOJI.get(severity, "❓")
    color = SEVERITY_COLOR.get(severity, "#AAAAAA")
    countries_str = ", ".join(countries) if countries else "Unknown"
    title = f"{(service or 'service')} · {(endpoint or action or 'anomaly')}"

    fields = [
        {"type": "mrkdwn", "text": f"*Service:*\n{service or 'unknown'}"},
        {"type": "mrkdwn", "text": f"*Endpoint:*\n`{endpoint or '—'}`"},
        {"type": "mrkdwn", "text": f"*Impact:*\n{affected_users} users, {event_count} events"},
        {"type": "mrkdwn", "text": f"*Region:*\n{countries_str}"},
        {"type": "mrkdwn", "text": f"*Score:*\n{score} / {severity}"},
        {"type": "mrkdwn", "text": f"*Owner:*\n{suggested_owner or 'Unknown'}"},
    ]
    if action:
        fields.append({"type": "mrkdwn", "text": f"*Action:*\n{action}"})
    if platform:
        fields.append({"type": "mrkdwn", "text": f"*Platform:*\n{platform}"})
    if provider:
        fields.append({"type": "mrkdwn", "text": f"*Provider:*\n{provider}"})

    blocks = [
        {"type": "header", "text": {
            "type": "plain_text",
            "text": f"{emoji} EARLYBIRD — {severity.upper()} INCIDENT",
            "emoji": True,
        }},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{title}*  ·  _{status}_"}},
        {"type": "divider"},
        {"type": "section", "fields": fields},
        {"type": "context", "elements": [{
            "type": "mrkdwn",
            "text": (
                f"🆔 `{str(incident_id)[:8]}`  |  "
                f"🔍 `{fingerprint}`  |  "
                f"🕐 first `{_iso(first_seen_at)}`  |  "
                f"🕑 latest `{_iso(last_seen_at)}`"
            ),
        }]},
        {"type": "context", "elements": [{
            "type": "mrkdwn",
            "text": (
                f"⏱️ *Agent Alert Timestamp:* `{_iso(agent_alert_timestamp)}`  |  "
                f"🤖 AI analysis: _{status}_  |  📋 Freshdesk: _Monitoring..._"
            ),
        }]},
    ]

    text = f"{emoji} {severity.upper()} incident on {title} (score {score})"
    return _deliver_with_retry(blocks, text, color)


# ─── Phase 2: LLM enrichment follow-up (AFTER delivery) ───────────────────────

def send_enrichment_followup(
    incident_id: str,
    severity: str,
    llm_summary: Optional[dict],
    thread_ts: Optional[str] = None,
) -> AlertDeliveryResult:
    """
    Post the LLM analysis as a follow-up. Threads under the first alert when a
    bot token + thread_ts are available; otherwise posts a standalone message
    referencing the incident id. Never raises — enrichment is best-effort.
    """
    if not llm_summary:
        return AlertDeliveryResult(delivered=False, error="no_summary")

    color = SEVERITY_COLOR.get(severity, "#AAAAAA")
    summary = llm_summary.get("summary", "")
    root_cause = llm_summary.get("suspected_root_cause", "")
    next_steps: List[str] = llm_summary.get("recommended_next_steps", []) or []
    support_msg = llm_summary.get("support_message", "")
    steps_text = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(next_steps[:5]))

    blocks = [
        {"type": "section", "text": {
            "type": "mrkdwn",
            "text": f"🤖 *AI Analysis for incident* `{str(incident_id)[:8]}`",
        }},
    ]
    if summary:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Summary:*\n{summary}"}})
    if root_cause:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Suspected Root Cause:*\n{root_cause}"}})
    if steps_text:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Suggested Next Steps:*\n{steps_text}"}})
    if support_msg:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Support Message:*\n_{support_msg}_"}})

    text = f"AI analysis for incident {str(incident_id)[:8]}"
    return _deliver_with_retry(blocks, text, color, thread_ts=thread_ts)


# ─── Backward-compatible full alert (single message with LLM inline) ──────────

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
    DEPRECATED single-shot alert (minimal + enrichment in one message).

    Retained for backward compatibility. The production pipeline now uses the
    faster two-phase send_immediate_alert() + send_enrichment_followup() so the
    benchmark timestamp is never gated on the LLM. Returns a truthy message id on
    delivery, else None.
    """
    first = send_immediate_alert(
        incident_id=incident_id, fingerprint=fingerprint, severity=severity,
        score=score, affected_users=affected_users, event_count=event_count,
        countries=countries, endpoint=endpoint, service=service,
        agent_alert_timestamp=agent_alert_timestamp, suggested_owner=suggested_owner,
        status="analysis below" if llm_summary else "no AI summary",
    )
    if not first.delivered:
        return None
    if llm_summary:
        send_enrichment_followup(
            incident_id=incident_id, severity=severity,
            llm_summary=llm_summary, thread_ts=first.thread_ts,
        )
    return first.message_id
