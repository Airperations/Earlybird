"""
Earlybird — Fallback notification channels (PagerDuty + email).

Slack is the primary alert channel (app/alerts/slack.py). For a 30-day production
trial a single channel is not enough: if Slack is down, the agent must still beat
support to the timestamp. These senders are the fallbacks tried, in order, only
when the higher-priority channel fails:

    Slack  →  PagerDuty  →  email

Each returns an AlertDeliveryResult with `channel` set, so the orchestrator can
record which channel actually delivered (and therefore which clock locked the
official benchmark timestamp). Both are no-ops returning `not_configured` when
their credentials are absent — never a crash, never a false "delivered".
"""

import logging
import smtplib
from email.message import EmailMessage
from typing import Optional

import requests

from app.alerts.slack import AlertDeliveryResult
from app.config import settings

logger = logging.getLogger(__name__)

# PagerDuty severity expects: critical | error | warning | info
_PD_SEVERITY = {
    "critical": "critical",
    "high": "error",
    "medium": "warning",
    "low": "info",
    "observe": "info",
}


def send_pagerduty_alert(
    *,
    incident_id: str,
    severity: str,
    summary: str,
    source: str = "earlybird",
    custom_details: Optional[dict] = None,
) -> AlertDeliveryResult:
    """
    Trigger a PagerDuty Events API v2 alert. `dedup_key` = incident id so retries
    or a later channel don't open duplicate PD incidents. Returns not_configured
    when no routing key is set.
    """
    if not settings.PAGERDUTY_ROUTING_KEY:
        return AlertDeliveryResult(delivered=False, channel="pagerduty", attempts=0, error="not_configured")

    payload = {
        "routing_key": settings.PAGERDUTY_ROUTING_KEY,
        "event_action": "trigger",
        "dedup_key": f"earlybird-{incident_id}",
        "payload": {
            "summary": summary[:1024],
            "source": source,
            "severity": _PD_SEVERITY.get(severity, "error"),
            "custom_details": custom_details or {},
        },
    }
    try:
        resp = requests.post(
            settings.PAGERDUTY_EVENTS_URL,
            json=payload,
            timeout=settings.PAGERDUTY_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") == "success":
            return AlertDeliveryResult(
                delivered=True, channel="pagerduty",
                message_id=body.get("dedup_key") or f"earlybird-{incident_id}", attempts=1,
            )
        return AlertDeliveryResult(delivered=False, channel="pagerduty", attempts=1,
                                   error=str(body.get("message", "pagerduty_error")))
    except requests.RequestException as e:
        logger.warning(f"[PAGERDUTY] delivery failed: {e}")
        return AlertDeliveryResult(delivered=False, channel="pagerduty", attempts=1, error=str(e))


def send_email_alert(
    *,
    incident_id: str,
    severity: str,
    subject: str,
    body: str,
) -> AlertDeliveryResult:
    """
    Send the alert by email via SMTP. Requires SMTP_HOST + ALERT_FALLBACK_EMAIL +
    ALERT_EMAIL_FROM; otherwise not_configured. STARTTLS is used when a username is
    provided.
    """
    if not (settings.SMTP_HOST and settings.ALERT_FALLBACK_EMAIL and settings.ALERT_EMAIL_FROM):
        return AlertDeliveryResult(delivered=False, channel="email", attempts=0, error="not_configured")

    msg = EmailMessage()
    msg["Subject"] = subject[:200]
    msg["From"] = settings.ALERT_EMAIL_FROM
    msg["To"] = settings.ALERT_FALLBACK_EMAIL
    msg.set_content(body)

    try:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=settings.SMTP_TIMEOUT_SECONDS) as smtp:
            if settings.SMTP_USERNAME:
                smtp.starttls()
                smtp.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD or "")
            smtp.send_message(msg)
        return AlertDeliveryResult(delivered=True, channel="email", message_id=f"email-{incident_id}", attempts=1)
    except Exception as e:  # noqa: BLE001 — any SMTP failure is a non-delivery, not a crash
        logger.warning(f"[EMAIL] delivery failed: {e}")
        return AlertDeliveryResult(delivered=False, channel="email", attempts=1, error=str(e))
