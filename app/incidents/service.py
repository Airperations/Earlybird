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

# Incident state machine valid transitions
STATE_TRANSITIONS = {
    "new": ["observing", "alerted", "ignored"],
    "observing": ["alerted", "resolved", "ignored"],
    "alerted": ["matched_to_freshdesk", "resolved", "false_positive"],
    "matched_to_freshdesk": ["resolved"],
    "resolved": ["new"],     # Can reopen
    "ignored": [],
    "false_positive": [],
}


async def find_or_create_incident(
    db: AsyncSession,
    normalized: NormalizedEventSchema,
) -> Incident:
    """Find an existing open incident with the same fingerprint or create a new one."""
    dedup_window = datetime.now(timezone.utc) - timedelta(
        minutes=settings.DEDUP_WINDOW_MINUTES
    )

    result = await db.execute(
        select(Incident)
        .where(Incident.fingerprint == normalized.fingerprint)
        .where(Incident.status.not_in(["resolved", "false_positive"]))
        .where(Incident.last_seen_at >= dedup_window)
    )
    incident = result.scalar_one_or_none()

    if incident:
        # Update existing incident
        incident.event_count += 1
        incident.last_seen_at = datetime.now(timezone.utc)

        # Merge countries
        countries = list(incident.countries or [])
        if normalized.country and normalized.country not in countries:
            countries.append(normalized.country)
            incident.countries = countries

        incident.updated_at = datetime.now(timezone.utc)
        logger.info(f"[INCIDENT] Updated existing incident {incident.id} (count={incident.event_count})")
    else:
        # Create new incident
        incident = Incident(
            id=uuid.uuid4(),
            fingerprint=normalized.fingerprint,
            status="new",
            first_seen_at=datetime.now(timezone.utc),
            last_seen_at=datetime.now(timezone.utc),
            event_count=1,
            affected_users_count=1 if normalized.user_id else 0,
            countries=[normalized.country] if normalized.country else [],
        )
        db.add(incident)
        await db.flush()
        logger.info(f"[INCIDENT] Created new incident {incident.id} for fingerprint {normalized.fingerprint}")

        await _log_audit(db, incident.id, "incident_created", {"fingerprint": normalized.fingerprint})

    return incident


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


async def mark_incident_alerted(
    db: AsyncSession,
    incident: Incident,
    slack_message_id: Optional[str],
    llm_summary: Optional[dict],
    title: Optional[str],
):
    """Mark incident as alerted — sets the KEY bounty timestamp."""
    incident.status = "alerted"
    incident.agent_alert_timestamp = datetime.now(timezone.utc)
    incident.slack_message_id = slack_message_id
    incident.llm_summary = llm_summary
    incident.title = title
    incident.updated_at = datetime.now(timezone.utc)

    await _log_audit(db, incident.id, "incident_alerted", {
        "agent_alert_timestamp": incident.agent_alert_timestamp.isoformat(),
        "severity": incident.severity,
        "score": incident.score,
    })

    logger.info(
        f"[INCIDENT] 🚨 Alert sent for {incident.id} at {incident.agent_alert_timestamp.isoformat()} "
        f"(score={incident.score}, severity={incident.severity})"
    )


async def _log_audit(db: AsyncSession, incident_id, event: str, details: dict):
    entry = AuditLog(
        incident_id=incident_id,
        event=event,
        details=details,
    )
    db.add(entry)
