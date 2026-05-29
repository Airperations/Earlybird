"""
Earlybird — Main Event Processing Worker
This is the brain of the agent. Runs asynchronously via Celery.

Full pipeline:
1. Save raw event
2. Normalize
3. Find/create incident
4. Score criticality
5. Decide if alert is needed
6. Generate LLM summary
7. Send Slack alert
8. Mark incident as alerted with timestamp
"""

import asyncio
import uuid
import logging
from datetime import datetime, timezone

import redis

from app.celery_app import celery_app
from app.database import AsyncSessionLocal
from app.models import RawEvent
from app.normalizers.base import normalize
from app.incidents import service as incident_service
from app.incidents.scoring import calculate_criticality
from app.llm.analyst import generate_incident_summary, build_incident_context
from app.alerts.slack import send_incident_alert
from app.config import settings

logger = logging.getLogger(__name__)

# Synchronous Redis client used only for the cross-worker alert lock below.
_redis = redis.Redis.from_url(settings.REDIS_URL)
ALERT_LOCK_TTL_SECONDS = 300


def _acquire_alert_lock(fingerprint: str) -> bool:
    """
    Best-effort distributed lock so a burst of identical events doesn't fire
    multiple Slack alerts / LLM calls in the window before the first event's
    status="alerted" commit lands. Fail-open: if Redis is unreachable we still
    alert (a duplicate alert beats a missed one).
    """
    try:
        return bool(_redis.set(f"earlybird:alert_lock:{fingerprint}", "1",
                               nx=True, ex=ALERT_LOCK_TTL_SECONDS))
    except Exception as e:
        logger.warning(f"[WORKER] Alert lock unavailable ({e}); proceeding without it")
        return True


def _release_alert_lock(fingerprint: str) -> None:
    try:
        _redis.delete(f"earlybird:alert_lock:{fingerprint}")
    except Exception:
        pass


@celery_app.task(
    name="app.workers.process_event.process_incoming_event",
    bind=True,
    max_retries=3,
    default_retry_delay=5,
    acks_late=True,
)
def process_incoming_event(self, source: str, payload: dict, received_at: str):
    """
    Main async processing pipeline wrapped for Celery.
    """
    try:
        asyncio.run(_process(source, payload, received_at))
    except Exception as exc:
        logger.error(f"[WORKER] Error processing event: {exc}", exc_info=True)
        raise self.retry(exc=exc)


async def _process(source: str, payload: dict, received_at_iso: str):
    received_at = datetime.fromisoformat(received_at_iso)

    async with AsyncSessionLocal() as db:
        # ── Step 1: Save raw event ───────────────────────────────────────────
        raw_event = RawEvent(
            id=uuid.uuid4(),
            source=source,
            received_at=received_at,
            raw_payload=payload,
            processed=False,
        )
        db.add(raw_event)
        await db.flush()
        raw_event_id = str(raw_event.id)
        logger.info(f"[WORKER] Raw event saved: {raw_event_id}")

        # ── Step 2: Normalize ────────────────────────────────────────────────
        try:
            normalized = normalize(source, payload)
        except Exception as e:
            logger.error(f"[WORKER] Normalization failed for {source}: {e}")
            await db.commit()
            return

        logger.info(f"[WORKER] Normalized: endpoint={normalized.endpoint} fingerprint={normalized.fingerprint}")

        # ── Step 3: Find or create incident ─────────────────────────────────
        incident = await incident_service.find_or_create_incident(db, normalized)

        # ── Step 4: Save normalized event + link to incident ─────────────────
        await incident_service.save_normalized_event(db, raw_event_id, normalized, incident)

        # ── Step 5: Calculate criticality score ──────────────────────────────
        scoring = calculate_criticality(
            event=normalized,
            affected_users=incident.affected_users_count,
            event_count=incident.event_count,
            countries=list(incident.countries or []),
            has_existing_tickets=False,  # Freshdesk check happens async
        )

        await incident_service.update_incident_score(db, incident, scoring)

        logger.info(
            f"[WORKER] Score: {scoring.total_score} | Severity: {scoring.severity} | "
            f"Breakdown: {scoring.breakdown}"
        )

        # ── Step 6: Decide whether to alert ─────────────────────────────────
        already_alerted = incident.status == "alerted"
        should_alert = (
            scoring.should_alert(threshold=settings.MEDIUM_SCORE_THRESHOLD)
            and not already_alerted
        )

        # Cross-worker guard: during a burst, several events for the same
        # fingerprint can pass the check before the first commits status="alerted".
        # Only the lock holder proceeds to alert; the rest skip (no duplicate Slack).
        if should_alert and not _acquire_alert_lock(normalized.fingerprint):
            logger.info(f"[WORKER] Alert lock held for {normalized.fingerprint} — skipping duplicate alert")
            should_alert = False

        if not should_alert:
            logger.info(f"[WORKER] Below threshold or already alerted — skipping alert")
            raw_event.processed = True
            await db.commit()
            return

        # Steps 7-9 hold the alert lock. If anything fails before the commit we
        # release it so a Celery retry can re-attempt the alert (otherwise the lock
        # would block re-alerting for its whole TTL and the incident would never alert).
        try:
            # ── Step 7: Generate LLM summary ─────────────────────────────────
            llm_context = build_incident_context(
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

            llm_summary = generate_incident_summary(llm_context)

            # ── Step 8: Send Slack alert ─────────────────────────────────────
            alert_timestamp = datetime.now(timezone.utc)

            slack_msg_id = send_incident_alert(
                incident_id=str(incident.id),
                fingerprint=normalized.fingerprint,
                severity=scoring.severity,
                score=scoring.total_score,
                affected_users=incident.affected_users_count,
                event_count=incident.event_count,
                countries=list(incident.countries or []),
                endpoint=normalized.endpoint,
                service=normalized.service,
                agent_alert_timestamp=alert_timestamp,
                llm_summary=llm_summary,
                suggested_owner=scoring.suggested_owner,
            )

            # ── Step 9: Mark incident as alerted (THE BOUNTY TIMESTAMP) ──────
            # Persist the SAME timestamp captured in Step 8 (before LLM/Slack), so
            # the DB value used by the matcher matches what Slack showed.
            title = llm_summary.get("title") if llm_summary else None
            await incident_service.mark_incident_alerted(
                db,
                incident,
                slack_msg_id,
                llm_summary,
                title,
                alert_timestamp=alert_timestamp,
                slack_delivered=slack_msg_id is not None,
            )

            raw_event.processed = True
            await db.commit()
        except Exception:
            _release_alert_lock(normalized.fingerprint)
            raise

        logger.info(f"[WORKER] ✅ Pipeline complete for incident {incident.id}")
