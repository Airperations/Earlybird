"""
Earlybird — Incident Service
Core business logic: find or create incidents, update state machine,
manage deduplication windows.
"""

import uuid
from datetime import datetime, timezone, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from typing import Optional
import logging

from app.models import Incident, NormalizedEvent, IncidentEvent, RawEvent, AuditLog
from app.normalizers.base import NormalizedEventSchema
from app.incidents.scoring import ScoringResult
from app.redaction import hash_identifier
from app.taxonomy import build_normalized_keywords
from app.config import settings

logger = logging.getLogger(__name__)

# An incident is "open" (and therefore reusable / blocking) in every status
# except these terminal ones. Mirrors the partial unique index in models.py and
# the official recurrence rule: a recurrence of a CLOSED fingerprint is a NEW race.
CLOSED_STATUSES = ("resolved", "false_positive")

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
    """
    Resolve the incident for an event under the official recurrence rule:

        same fingerprint + OPEN incident      → reuse it (ordinary dedup)
        same fingerprint + resolved/false_pos → create a NEW incident row

    Each recurrence of a CLOSED fingerprint is therefore its own benchmark race
    against Freshdesk — clean incident_id, first_seen, alert timestamp, and match.
    The partial unique index guarantees at most one open incident per fingerprint.
    """
    # 1) Reuse the single OPEN incident for this fingerprint, if any.
    open_incident = (await db.execute(
        select(Incident)
        .where(Incident.fingerprint == normalized.fingerprint)
        .where(Incident.status.not_in(CLOSED_STATUSES))
        .order_by(Incident.first_seen_at.desc())
    )).scalars().first()

    if open_incident:
        _absorb_event(open_incident, normalized)
        logger.info(f"[INCIDENT] Updated open incident {open_incident.id} (count={open_incident.event_count})")
        return open_incident

    # 2) No open incident. If a CLOSED one exists, this is a recurrence → new row.
    prior = (await db.execute(
        select(Incident)
        .where(Incident.fingerprint == normalized.fingerprint)
        .order_by(Incident.first_seen_at.desc())
    )).scalars().first()
    is_recurrence = prior is not None

    now = datetime.now(timezone.utc)
    user_hash = hash_identifier(normalized.user_id)
    incident = Incident(
        id=uuid.uuid4(),
        fingerprint=normalized.fingerprint,
        status="new",
        first_seen_at=now,
        last_seen_at=now,
        event_count=1,
        affected_users_count=1 if user_hash else 0,
        affected_user_hashes=[user_hash] if user_hash else [],
        countries=[normalized.country] if normalized.country else [],
        **_incident_metadata(normalized),
    )

    # The partial unique index can reject this insert if a concurrent worker just
    # created the open incident for the same fingerprint. Guard with a savepoint
    # and fall back to reusing that incident — no IntegrityError ever surfaces.
    try:
        async with db.begin_nested():
            db.add(incident)
            await db.flush()
    except IntegrityError:
        await db.rollback()
        raced = (await db.execute(
            select(Incident)
            .where(Incident.fingerprint == normalized.fingerprint)
            .where(Incident.status.not_in(CLOSED_STATUSES))
            .order_by(Incident.first_seen_at.desc())
        )).scalars().first()
        if raced:
            _absorb_event(raced, normalized)
            logger.info(f"[INCIDENT] Lost open-incident race for {normalized.fingerprint}; reusing {raced.id}")
            return raced
        raise

    if is_recurrence:
        await _log_audit(db, incident.id, "incident_recurred", {
            "fingerprint": normalized.fingerprint,
            "previous_incident_id": str(prior.id),
            "previous_status": prior.status,
        })
        logger.info(
            f"[INCIDENT] ♻️ Recurrence — NEW incident {incident.id} for fingerprint "
            f"{normalized.fingerprint} (prior {prior.id} was {prior.status})"
        )
    else:
        logger.info(f"[INCIDENT] Created new incident {incident.id} for fingerprint {normalized.fingerprint}")

    await _log_audit(db, incident.id, "incident_created", {
        "fingerprint": normalized.fingerprint,
        "recurrence": is_recurrence,
        "business_action": normalized.business_action,
    })

    return incident


def _incident_metadata(normalized: NormalizedEventSchema) -> dict:
    """Structured business metadata copied onto a new incident from its first event."""
    return dict(
        # A normalize-time title (e.g. Stellar structured logs) gives the incident
        # a readable name pre-LLM; the LLM enrichment still overwrites it later.
        # None for sources that don't set one, so their behaviour is unchanged.
        title=normalized.title,
        service=normalized.service,
        endpoint=normalized.endpoint,
        route=normalized.endpoint,
        business_action=normalized.business_action,
        http_status=normalized.http_status,
        exception_type=normalized.exception_type,
        primary_country=normalized.country,
        provider=normalized.provider,
        platform=normalized.platform,
        payment_method=normalized.payment_method,
        normalized_keywords=build_normalized_keywords(
            business_action=normalized.business_action,
            provider=normalized.provider,
            payment_method=normalized.payment_method,
            country=normalized.country,
            exception_type=normalized.exception_type,
            endpoint=normalized.endpoint,
        ),
    )


def _absorb_event(incident: Incident, normalized: NormalizedEventSchema) -> None:
    """Fold one more event into an existing incident: count, recency, countries, users, metadata."""
    incident.event_count += 1
    incident.last_seen_at = datetime.now(timezone.utc)

    countries = list(incident.countries or [])
    if normalized.country and normalized.country not in countries:
        countries.append(normalized.country)
        incident.countries = countries

    # Track distinct affected users by SALTED HASH — never the raw id.
    user_hash = hash_identifier(normalized.user_id)
    if user_hash:
        users = list(incident.affected_user_hashes or [])
        if user_hash not in users:
            users.append(user_hash)
            incident.affected_user_hashes = users
            incident.affected_users_count = len(users)

    # Backfill structured metadata that the first event didn't carry (e.g. a later
    # event names the provider / payment method), without overwriting known values.
    for field in ("service", "endpoint", "business_action", "http_status",
                  "exception_type", "provider", "platform", "payment_method"):
        if getattr(incident, field, None) in (None, "") and getattr(normalized, field, None):
            setattr(incident, field, getattr(normalized, field))
    if not incident.primary_country and normalized.country:
        incident.primary_country = normalized.country
    if not incident.route and normalized.endpoint:
        incident.route = normalized.endpoint
    incident.normalized_keywords = build_normalized_keywords(
        business_action=incident.business_action,
        provider=incident.provider,
        payment_method=incident.payment_method,
        country=incident.primary_country,
        exception_type=incident.exception_type,
        endpoint=incident.endpoint,
    )

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
        user_id=hash_identifier(normalized.user_id),   # store the salted hash, never the raw id
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
    channel: str = "slack",
    channel_log: Optional[list] = None,
):
    """
    Record a CONFIRMED first-alert delivery — the moment that sets the official
    benchmark timestamp.

    CRITICAL INVARIANT: agent_alert_timestamp is assigned here and ONLY here,
    mirroring notification_delivered_at. It is therefore impossible for the agent
    to claim a win without an actual delivered notification. The timestamp is the
    delivery time of the FIRST channel that succeeded (Slack → PagerDuty → email).
    No LLM output is required at this point — enrichment happens afterward.
    """
    incident.notification_attempted_at = incident.notification_attempted_at or delivered_at
    incident.notification_delivered_at = delivered_at
    incident.agent_alert_timestamp = delivered_at          # ← the bounty field
    incident.notification_status = "delivered"
    incident.notification_attempts = attempts
    incident.notification_channel = channel
    # Only Slack returns a thread/message ts; keep them channel-appropriate.
    if channel == "slack":
        incident.slack_message_id = slack_message_id
        incident.slack_thread_ts = thread_ts
    incident.updated_at = datetime.now(timezone.utc)

    await transition_incident(db, incident, "alerted", {
        "agent_alert_timestamp": delivered_at.isoformat(),
        "delivered": True,
        "channel": channel,
        "attempts": attempts,
        "channel_log": channel_log or [],
    })

    logger.info(
        f"[INCIDENT] 🚨 Alert DELIVERED for {incident.id} via {channel} at {delivered_at.isoformat()} "
        f"(score={incident.score}, severity={incident.severity}, attempts={attempts})"
    )


async def mark_notification_failed(
    db: AsyncSession,
    incident: Incident,
    attempted_at: datetime,
    attempts: int,
    error: Optional[str] = None,
    channel_log: Optional[list] = None,
):
    """
    Record that delivery failed on EVERY channel after all retries. The benchmark
    timestamp stays NULL — a failed alert is NEVER counted as a win, and the
    per-channel failure log is auditable.
    """
    incident.notification_attempted_at = attempted_at
    incident.notification_status = "failed"
    incident.notification_attempts = attempts
    incident.updated_at = datetime.now(timezone.utc)

    await transition_incident(db, incident, "notification_failed", {
        "attempts": attempts,
        "error": (error or "")[:500],
        "channel_log": channel_log or [],
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
