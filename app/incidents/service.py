"""
Earlybird — Incident Service
Core business logic: find or create incidents, update state machine,
manage deduplication windows.
"""

import uuid
from datetime import datetime, timezone, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from typing import Optional
import logging

from app.models import Incident, NormalizedEvent, IncidentEvent, RawEvent, AuditLog
from app.normalizers.base import NormalizedEventSchema
from app.incidents.scoring import ScoringResult
from app.config import settings

logger = logging.getLogger(__name__)

# Incident state machine valid transitions.
#
#   new ─▶ observing ─▶ detected ─▶ alerted ─▶ enriched ─▶ matched_to_freshdesk ─▶ resolved
#                          │           │
#                          ▼           ▼
#                  notification_failed  (matched/resolved/false_positive)
#
#   detected:            crossed the alert threshold (decision made, not yet delivered)
#   alerted:             first Slack alert DELIVERED → benchmark timestamp set
#   enriched:            LLM analysis posted as a follow-up after delivery
#   notification_failed: Slack delivery failed after all retries (NO win recorded)
#   matched_to_freshdesk: a related support ticket was found (race resolved)
STATE_TRANSITIONS = {
    "new": ["observing", "detected", "alerted", "ignored"],
    "observing": ["detected", "alerted", "resolved", "ignored"],
    "detected": ["alerted", "notification_failed", "ignored"],
    "alerted": ["enriched", "matched_to_freshdesk", "resolved", "false_positive"],
    "enriched": ["matched_to_freshdesk", "resolved", "false_positive"],
    "notification_failed": ["detected", "alerted", "ignored"],  # retryable
    "matched_to_freshdesk": ["resolved"],
    "resolved": ["new"],     # Can reopen on recurrence
    "ignored": [],
    "false_positive": [],
}


def can_transition(current: str, new: str) -> bool:
    """True if `current → new` is a legal state-machine edge (self-edges allowed)."""
    if current == new:
        return True
    return new in STATE_TRANSITIONS.get(current, [])


async def transition_incident(
    db: AsyncSession,
    incident: Incident,
    new_status: str,
    details: Optional[dict] = None,
) -> bool:
    """
    Move an incident to `new_status`, validating the transition and writing an
    audit entry. Returns False (and logs) on an illegal transition instead of
    raising, so a bad edge never crashes the pipeline mid-alert.
    """
    current = incident.status
    if new_status == current:
        return True
    if not can_transition(current, new_status):
        logger.warning(f"[INCIDENT] Illegal transition {current} → {new_status} for {incident.id}")
        return False

    incident.status = new_status
    incident.updated_at = datetime.now(timezone.utc)
    await _log_audit(db, incident.id, "state_transition", {
        "from": current,
        "to": new_status,
        **(details or {}),
    })
    return True


async def find_or_create_incident(
    db: AsyncSession,
    normalized: NormalizedEventSchema,
) -> Incident:
    """Find an existing open incident with the same fingerprint or create a new one."""
    dedup_window = datetime.now(timezone.utc) - timedelta(
        minutes=settings.DEDUP_WINDOW_MINUTES
    )

    # Active incident within the dedup window → ordinary update (deduplication).
    result = await db.execute(
        select(Incident)
        .where(Incident.fingerprint == normalized.fingerprint)
        .where(Incident.status.not_in(["resolved", "false_positive"]))
        .where(Incident.last_seen_at >= dedup_window)
    )
    incident = result.scalar_one_or_none()

    if incident:
        _absorb_event(incident, normalized)
        logger.info(f"[INCIDENT] Updated existing incident {incident.id} (count={incident.event_count})")
        return incident

    # No active incident in-window. Because `fingerprint` is globally unique, a
    # row may still exist that is resolved, a false positive, or simply went quiet
    # past the dedup window. That is a RECURRENCE — reopen and re-arm it rather
    # than (illegally) inserting a duplicate fingerprint.
    existing = await db.execute(
        select(Incident).where(Incident.fingerprint == normalized.fingerprint)
    )
    incident = existing.scalar_one_or_none()
    if incident:
        previous_status = incident.status
        _absorb_event(incident, normalized)
        # Reset the race fields so the recurrence is alerted (and raced) afresh.
        if previous_status in ("resolved", "false_positive", "matched_to_freshdesk",
                               "alerted", "enriched", "notification_failed"):
            incident.status = "new"
            incident.agent_alert_timestamp = None
            incident.notification_status = "pending"
            incident.notification_attempted_at = None
            incident.notification_delivered_at = None
            incident.detected_at = None
            incident.enriched_at = None
        await _log_audit(db, incident.id, "incident_recurred", {
            "fingerprint": normalized.fingerprint,
            "previous_status": previous_status,
        })
        logger.info(f"[INCIDENT] ♻️ Recurrence — reopened incident {incident.id} (was {previous_status})")
        return incident

    # Genuinely new fingerprint → create.
    incident = Incident(
        id=uuid.uuid4(),
        fingerprint=normalized.fingerprint,
        status="new",
        first_seen_at=datetime.now(timezone.utc),
        last_seen_at=datetime.now(timezone.utc),
        event_count=1,
        affected_users_count=1 if normalized.user_id else 0,
        affected_user_ids=[normalized.user_id] if normalized.user_id else [],
        countries=[normalized.country] if normalized.country else [],
    )
    db.add(incident)
    await db.flush()
    logger.info(f"[INCIDENT] Created new incident {incident.id} for fingerprint {normalized.fingerprint}")

    await _log_audit(db, incident.id, "incident_created", {"fingerprint": normalized.fingerprint})

    return incident


def _absorb_event(incident: Incident, normalized: NormalizedEventSchema) -> None:
    """Fold one more event into an existing incident: count, recency, countries, users."""
    incident.event_count += 1
    incident.last_seen_at = datetime.now(timezone.utc)

    countries = list(incident.countries or [])
    if normalized.country and normalized.country not in countries:
        countries.append(normalized.country)
        incident.countries = countries

    # Track distinct affected users so the "affected_users" scoring tier is real.
    if normalized.user_id:
        users = list(incident.affected_user_ids or [])
        if normalized.user_id not in users:
            users.append(normalized.user_id)
            incident.affected_user_ids = users
            incident.affected_users_count = len(users)

    incident.updated_at = datetime.now(timezone.utc)


async def save_normalized_event(
    db: AsyncSession,
    raw_event_id: str,
    normalized: NormalizedEventSchema,
    incident: Incident,
) -> NormalizedEvent:
    """Persist the normalized event and link it to the incident."""
    norm_event = NormalizedEvent(
        id=uuid.uuid4(),
        raw_event_id=uuid.UUID(raw_event_id) if raw_event_id else None,
        source=normalized.source,
        service=normalized.service,
        environment=normalized.environment,
        endpoint=normalized.endpoint,
        url=normalized.url,
        http_status=normalized.http_status,
        exception_type=normalized.exception_type,
        message=normalized.message,
        user_id=normalized.user_id,
        country=normalized.country,
        platform=normalized.platform,
        release=normalized.release,
        fingerprint=normalized.fingerprint,
    )
    db.add(norm_event)
    await db.flush()

    link = IncidentEvent(
        id=uuid.uuid4(),
        incident_id=incident.id,
        normalized_event_id=norm_event.id,
    )
    db.add(link)

    return norm_event


async def update_incident_score(
    db: AsyncSession,
    incident: Incident,
    scoring: ScoringResult,
):
    """Update incident severity and score."""
    incident.score = scoring.total_score
    incident.severity = scoring.severity
    incident.updated_at = datetime.now(timezone.utc)

    # Transition state based on score
    if scoring.total_score >= settings.MEDIUM_SCORE_THRESHOLD:
        if incident.status == "new":
            incident.status = "observing"


async def mark_detected(db: AsyncSession, incident: Incident, detected_at: datetime, score: int):
    """Record that the incident crossed the alert threshold (pre-delivery)."""
    incident.detected_at = detected_at
    incident.updated_at = datetime.now(timezone.utc)
    await transition_incident(db, incident, "detected", {"score": score})


async def mark_incident_delivered(
    db: AsyncSession,
    incident: Incident,
    slack_message_id: Optional[str],
    thread_ts: Optional[str],
    delivered_at: datetime,
    attempts: int,
):
    """
    Record a CONFIRMED first-alert delivery — the moment that sets the official
    benchmark timestamp.

    CRITICAL INVARIANT: agent_alert_timestamp is assigned here and ONLY here,
    mirroring notification_delivered_at. It is therefore impossible for the agent
    to claim a win without an actual delivered notification. No LLM output is
    required at this point — enrichment happens afterward.
    """
    incident.notification_attempted_at = incident.notification_attempted_at or delivered_at
    incident.notification_delivered_at = delivered_at
    incident.agent_alert_timestamp = delivered_at          # ← the bounty field
    incident.notification_status = "delivered"
    incident.notification_attempts = attempts
    incident.slack_message_id = slack_message_id
    incident.slack_thread_ts = thread_ts
    incident.updated_at = datetime.now(timezone.utc)

    await transition_incident(db, incident, "alerted", {
        "agent_alert_timestamp": delivered_at.isoformat(),
        "slack_delivered": True,
        "attempts": attempts,
    })

    logger.info(
        f"[INCIDENT] 🚨 Alert DELIVERED for {incident.id} at {delivered_at.isoformat()} "
        f"(score={incident.score}, severity={incident.severity}, attempts={attempts})"
    )


async def mark_notification_failed(
    db: AsyncSession,
    incident: Incident,
    attempted_at: datetime,
    attempts: int,
    error: Optional[str] = None,
):
    """
    Record that delivery failed after all retries. The benchmark timestamp stays
    NULL — a failed alert is NEVER counted as a win, and the failure is auditable.
    """
    incident.notification_attempted_at = attempted_at
    incident.notification_status = "failed"
    incident.notification_attempts = attempts
    incident.updated_at = datetime.now(timezone.utc)

    await transition_incident(db, incident, "notification_failed", {
        "attempts": attempts,
        "error": (error or "")[:500],
    })

    logger.error(
        f"[INCIDENT] ❌ Notification FAILED for {incident.id} after {attempts} attempts — "
        f"no benchmark timestamp recorded"
    )


async def mark_incident_enriched(
    db: AsyncSession,
    incident: Incident,
    llm_summary: Optional[dict],
    title: Optional[str],
    enriched_at: datetime,
):
    """
    Attach the LLM analysis produced AFTER the first alert was delivered. Moves
    the incident to `enriched`. Never touches agent_alert_timestamp.
    """
    incident.llm_summary = llm_summary
    if title:
        incident.title = title
    incident.enriched_at = enriched_at
    incident.updated_at = datetime.now(timezone.utc)
    await transition_incident(db, incident, "enriched", {
        "has_summary": bool(llm_summary),
    })


async def mark_incident_alerted(
    db: AsyncSession,
    incident: Incident,
    slack_message_id: Optional[str],
    llm_summary: Optional[dict],
    title: Optional[str],
    alert_timestamp: datetime,
    slack_delivered: bool,
):
    """
    DEPRECATED compatibility shim for the old single-phase flow. Delegates to the
    new delivery/enrichment functions. Prefer mark_incident_delivered() +
    mark_incident_enriched() directly. Only sets the benchmark timestamp when the
    alert was actually delivered.
    """
    if slack_delivered:
        await mark_incident_delivered(
            db, incident, slack_message_id, thread_ts=None,
            delivered_at=alert_timestamp, attempts=1,
        )
        if llm_summary or title:
            await mark_incident_enriched(db, incident, llm_summary, title, alert_timestamp)
    else:
        await mark_notification_failed(db, incident, alert_timestamp, attempts=1)


async def _log_audit(db: AsyncSession, incident_id, event: str, details: dict):
    entry = AuditLog(
        incident_id=incident_id,
        event=event,
        details=details,
    )
    db.add(entry)
