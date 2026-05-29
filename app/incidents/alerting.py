"""
Earlybird — Fast-path Alert Orchestrator

The single most important product principle lives here:

    score → minimal immediate alert → REAL delivered timestamp → LLM enrichment
          → thread follow-up

The first alert carries no AI text and is never gated on the LLM, so the official
benchmark timestamp (incident.agent_alert_timestamp) is set the instant Slack
confirms delivery. Enrichment runs strictly afterward and can fail without
costing the incident its "alerted" status or its win.

The orchestration is split into two awaitable steps so the worker can COMMIT the
delivered timestamp before enrichment begins (durability: a crash during
enrichment can never lose a delivered win). All external calls are injected so
the ordering can be proven in tests without real Slack/LLM.
"""

import logging
from datetime import datetime, timezone
from typing import Callable, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.incidents import service
from app.incidents.scoring import ScoringResult
from app.normalizers.base import NormalizedEventSchema
from app.alerts.slack import (
    send_immediate_alert,
    send_enrichment_followup,
    AlertDeliveryResult,
)
from app.alerts.channels import send_pagerduty_alert, send_email_alert
from app.llm.analyst import generate_incident_summary, build_incident_context
from app.redaction import redact_summary

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _fallback_content(incident, normalized, scoring):
    """Build the summary/details/body the PagerDuty + email fallbacks need."""
    countries = ", ".join(incident.countries or []) or "unknown"
    summary = (
        f"[{scoring.severity.upper()}] {normalized.service or 'service'} · "
        f"{normalized.business_action or normalized.endpoint or 'anomaly'} "
        f"({incident.affected_users_count} users, {incident.event_count} events, {countries})"
    )
    details = {
        "incident_id": str(incident.id),
        "fingerprint": normalized.fingerprint,
        "business_action": normalized.business_action,
        "service": normalized.service,
        "endpoint": normalized.endpoint,
        "http_status": normalized.http_status,
        "score": scoring.total_score,
        "severity": scoring.severity,
        "countries": list(incident.countries or []),
        "provider": normalized.provider,
    }
    body = summary + "\n\n" + "\n".join(f"{k}: {v}" for k, v in details.items())
    return summary, details, f"Earlybird {scoring.severity.upper()}: {normalized.business_action or normalized.endpoint}", body


def _deliver_multichannel(
    incident, normalized, scoring, *,
    send_alert, send_pagerduty, send_email,
):
    """
    Try Slack → PagerDuty → email, stopping at the first confirmed delivery.
    Returns (result, channel_log). `result` is the delivering channel's result, or
    the Slack result if every channel failed (so the failure record keeps Slack's
    attempt count). The first delivered channel is what locks the timestamp.
    """
    channel_log = []

    primary = send_alert(
        incident_id=str(incident.id),
        fingerprint=normalized.fingerprint,
        severity=scoring.severity,
        score=scoring.total_score,
        affected_users=incident.affected_users_count,
        event_count=incident.event_count,
        countries=list(incident.countries or []),
        endpoint=normalized.endpoint,
        service=normalized.service,
        action=normalized.business_action or normalized.exception_type,
        platform=normalized.platform,
        provider=normalized.provider,
        first_seen_at=incident.first_seen_at,
        last_seen_at=incident.last_seen_at,
        suggested_owner=scoring.suggested_owner,
        status="enriching…",
    )
    primary.channel = primary.channel or "slack"
    channel_log.append({"channel": "slack", "delivered": primary.delivered,
                        "attempts": primary.attempts, "error": primary.error})
    if primary.delivered:
        return primary, channel_log

    # Slack failed → fallbacks. Build their content once.
    summary, details, subject, body = _fallback_content(incident, normalized, scoring)

    pd = send_pagerduty(incident_id=str(incident.id), severity=scoring.severity,
                        summary=summary, custom_details=details)
    channel_log.append({"channel": "pagerduty", "delivered": pd.delivered,
                        "attempts": pd.attempts, "error": pd.error})
    if pd.delivered:
        return pd, channel_log

    em = send_email(incident_id=str(incident.id), severity=scoring.severity,
                    subject=subject, body=body)
    channel_log.append({"channel": "email", "delivered": em.delivered,
                        "attempts": em.attempts, "error": em.error})
    if em.delivered:
        return em, channel_log

    # Everything failed — return the Slack result so attempts/error reflect the
    # primary channel, with the full per-channel log for the audit trail.
    return primary, channel_log


async def deliver_alert(
    db: AsyncSession,
    incident,
    normalized: NormalizedEventSchema,
    scoring: ScoringResult,
    *,
    send_alert: Callable[..., AlertDeliveryResult] = send_immediate_alert,
    send_pagerduty: Callable[..., AlertDeliveryResult] = send_pagerduty_alert,
    send_email: Callable[..., AlertDeliveryResult] = send_email_alert,
    now: Callable[[], datetime] = _utcnow,
) -> AlertDeliveryResult:
    """
    PHASE 1 — detect → minimal alert (Slack → PagerDuty → email) → record outcome.
    NO LLM is called.

    On the FIRST confirmed delivery (any channel) the official benchmark timestamp
    is set and the delivering channel is recorded. If every channel fails the
    incident moves to notification_failed and the timestamp stays NULL.
    """
    detected_at = now()
    await service.mark_detected(db, incident, detected_at, scoring.total_score)

    result, channel_log = _deliver_multichannel(
        incident, normalized, scoring,
        send_alert=send_alert, send_pagerduty=send_pagerduty, send_email=send_email,
    )

    attempted_at = now()
    incident.notification_attempted_at = attempted_at

    if result.delivered:
        delivered_at = now()
        await service.mark_incident_delivered(
            db, incident,
            slack_message_id=result.message_id,
            thread_ts=result.thread_ts,
            delivered_at=delivered_at,
            attempts=result.attempts,
            channel=result.channel or "slack",
            channel_log=channel_log,
        )
    else:
        await service.mark_notification_failed(
            db, incident, attempted_at, attempts=result.attempts, error=result.error,
            channel_log=channel_log,
        )

    return result


async def enrich_incident(
    db: AsyncSession,
    incident,
    normalized: NormalizedEventSchema,
    scoring: ScoringResult,
    *,
    generate_summary: Callable[[dict], Optional[dict]] = generate_incident_summary,
    send_followup: Callable[..., AlertDeliveryResult] = send_enrichment_followup,
    thread_ts: Optional[str] = None,
    now: Callable[[], datetime] = _utcnow,
) -> Optional[dict]:
    """
    PHASE 2 — LLM enrichment AFTER the first alert was delivered. Best-effort:
    any failure here leaves the incident 'alerted' (the win stands) and is logged.
    Never raises.
    """
    summary = None
    try:
        context = build_incident_context(
            fingerprint=normalized.fingerprint,
            service=normalized.service,
            endpoint=normalized.endpoint,
            http_status=normalized.http_status,
            affected_users=incident.affected_users_count,
            event_count=incident.event_count,
            countries=list(incident.countries or []),
            severity=scoring.severity,
            score=scoring.total_score,
            message=normalized.message,
            exception_type=normalized.exception_type,
            first_seen_at=incident.first_seen_at.isoformat(),
            last_seen_at=incident.last_seen_at.isoformat(),
        )
        summary = redact_summary(generate_summary(context))
    except Exception as e:  # noqa: BLE001 — enrichment must never break a delivered alert
        logger.error(f"[ALERTING] Enrichment generation failed for {incident.id}: {e}")
        summary = None

    if summary:
        try:
            send_followup(
                incident_id=str(incident.id),
                severity=scoring.severity,
                llm_summary=summary,
                thread_ts=thread_ts or incident.slack_thread_ts,
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"[ALERTING] Enrichment follow-up post failed for {incident.id}: {e}")
        await service.mark_incident_enriched(
            db, incident, summary, summary.get("title"), now(),
        )
    else:
        # Enrichment produced nothing — the alert still counts; record the attempt.
        await service._log_audit(db, incident.id, "enrichment_failed", {"reason": "no_summary"})

    return summary


async def run_alert_pipeline(
    db: AsyncSession,
    incident,
    normalized: NormalizedEventSchema,
    scoring: ScoringResult,
    *,
    send_alert: Callable[..., AlertDeliveryResult] = send_immediate_alert,
    generate_summary: Callable[[dict], Optional[dict]] = generate_incident_summary,
    send_followup: Callable[..., AlertDeliveryResult] = send_enrichment_followup,
    now: Callable[[], datetime] = _utcnow,
) -> AlertDeliveryResult:
    """
    Convenience composition (deliver → enrich) without intermediate commits. The
    production worker calls deliver_alert() and enrich_incident() separately so it
    can commit the delivered timestamp before enriching, but this single entry
    point is handy for tests and simple callers. Enrichment runs ONLY after a
    confirmed delivery.
    """
    result = await deliver_alert(
        db, incident, normalized, scoring, send_alert=send_alert, now=now,
    )
    if result.delivered:
        await enrich_incident(
            db, incident, normalized, scoring,
            generate_summary=generate_summary, send_followup=send_followup,
            thread_ts=result.thread_ts, now=now,
        )
    return result
