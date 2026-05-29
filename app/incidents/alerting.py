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
from app.llm.analyst import generate_incident_summary, build_incident_context
from app.redaction import redact_summary

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def deliver_alert(
    db: AsyncSession,
    incident,
    normalized: NormalizedEventSchema,
    scoring: ScoringResult,
    *,
    send_alert: Callable[..., AlertDeliveryResult] = send_immediate_alert,
    now: Callable[[], datetime] = _utcnow,
) -> AlertDeliveryResult:
    """
    PHASE 1 — detect → minimal alert → record delivery outcome. NO LLM is called.

    On confirmed delivery the official benchmark timestamp is set. On failure the
    incident moves to notification_failed and the timestamp stays NULL.
    """
    detected_at = now()
    await service.mark_detected(db, incident, detected_at, scoring.total_score)

    result = send_alert(
        incident_id=str(incident.id),
        fingerprint=normalized.fingerprint,
        severity=scoring.severity,
        score=scoring.total_score,
        affected_users=incident.affected_users_count,
        event_count=incident.event_count,
        countries=list(incident.countries or []),
        endpoint=normalized.endpoint,
        service=normalized.service,
        action=normalized.exception_type,
        platform=normalized.platform,
        first_seen_at=incident.first_seen_at,
        last_seen_at=incident.last_seen_at,
        suggested_owner=scoring.suggested_owner,
        status="enriching…",
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
        )
    else:
        await service.mark_notification_failed(
            db, incident, attempted_at, attempts=result.attempts, error=result.error,
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
