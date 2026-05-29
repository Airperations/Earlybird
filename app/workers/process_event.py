"""
Earlybird — Main Event Processing Worker
This is the brain of the agent. Runs asynchronously via Celery.

Full pipeline (fast-path alerting):
1. Save raw event (idempotent)
2. Normalize
3. Find/create incident
4. Score criticality (+ anomaly detection)
5. Decide if alert is needed (configurable thresholds, critical-path bar, anomaly)
6. Send MINIMAL Slack alert immediately — NO LLM first
7. On confirmed delivery, set the official benchmark timestamp + COMMIT
8. Generate LLM enrichment AFTER delivery, post as a thread follow-up + COMMIT

The benchmark timestamp is never gated on the LLM, so the agent's alert lands as
early as possible — the whole point of the challenge.
"""

import asyncio
import uuid
import json
import hashlib
import logging
from datetime import datetime, timezone

import redis
from sqlalchemy import select

from app.celery_app import celery_app
from app.database import AsyncSessionLocal
from app.models import RawEvent, DeadLetterEvent
from app.normalizers.base import normalize
from app.incidents import service as incident_service
from app.incidents import alerting
from app.incidents.scoring import calculate_criticality, _score_to_severity
from app.incidents.anomaly import detect_anomaly
from app.config import settings

logger = logging.getLogger(__name__)

# Synchronous Redis client used for the cross-worker alert lock and the
# sliding-window error-velocity counter below.
_redis = redis.Redis.from_url(settings.REDIS_URL)
ALERT_LOCK_TTL_SECONDS = 300
VELOCITY_WINDOW_SECONDS = 300  # 5 minutes


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


def _idempotency_key(source: str, received_at_iso: str, payload: dict) -> str:
    """Deterministic across redeliveries: same (source, timestamp, payload) → same key."""
    blob = f"{source}|{received_at_iso}|{json.dumps(payload, sort_keys=True, default=str)}"
    return hashlib.sha256(blob.encode()).hexdigest()


def _record_and_count_velocity(fingerprint: str, member: str, now_epoch: float):
    """
    True sliding-window error rate: track event timestamps for a fingerprint in a
    Redis sorted set, trim everything older than the window, and return how many
    remain. This distinguishes a real burst from a slow trickle, unlike dividing a
    cumulative count by a fixed window. Returns None on Redis error (caller falls
    back to the cumulative event_count).
    """
    key = f"earlybird:velocity:{fingerprint}"
    try:
        pipe = _redis.pipeline()
        pipe.zadd(key, {member: now_epoch})
        pipe.zremrangebyscore(key, 0, now_epoch - VELOCITY_WINDOW_SECONDS)
        pipe.zcard(key)
        pipe.expire(key, VELOCITY_WINDOW_SECONDS)
        results = pipe.execute()
        return int(results[2])
    except Exception as e:
        logger.warning(f"[WORKER] Velocity window unavailable ({e}); using cumulative count")
        return None


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
        try:
            # Re-raise as a retry. On non-final attempts this raises Retry and
            # Celery reschedules. On the final attempt it raises MaxRetriesExceededError.
            raise self.retry(exc=exc)
        except self.MaxRetriesExceededError:
            logger.critical(f"[WORKER] Event exhausted retries — sending to dead-letter queue")
            try:
                asyncio.run(_save_dead_letter(source, payload, received_at, str(exc)))
            except Exception as dl_exc:
                logger.critical(f"[WORKER] FAILED to write dead-letter event: {dl_exc}")
            # Do not re-raise: the event is now durably captured in the DLQ, so the
            # task can be acked instead of redelivered forever.


async def _save_dead_letter(source: str, payload: dict, received_at_iso: str, error: str):
    try:
        received_at = datetime.fromisoformat(received_at_iso)
    except Exception:
        received_at = None
    async with AsyncSessionLocal() as db:
        db.add(DeadLetterEvent(
            id=uuid.uuid4(),
            source=source,
            raw_payload=payload,
            received_at=received_at,
            error=error[:2000],
        ))
        await db.commit()


async def _process(source: str, payload: dict, received_at_iso: str):
    received_at = datetime.fromisoformat(received_at_iso)

    idem_key = _idempotency_key(source, received_at_iso, payload)

    async with AsyncSessionLocal() as db:
        # ── Step 1: Save raw event (idempotent on redelivery) ────────────────
        existing = await db.execute(
            select(RawEvent).where(RawEvent.idempotency_key == idem_key)
        )
        raw_event = existing.scalar_one_or_none()
        if raw_event is not None:
            if raw_event.processed:
                logger.info(f"[WORKER] Duplicate delivery {idem_key[:12]} already processed — skipping")
                return
            # Partially-processed leftover: reuse the row instead of inserting a twin.
            logger.info(f"[WORKER] Reprocessing unfinished raw event {raw_event.id}")
        else:
            raw_event = RawEvent(
                id=uuid.uuid4(),
                source=source,
                received_at=received_at,
                raw_payload=payload,
                processed=False,
                idempotency_key=idem_key,
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
        # Sliding-window error rate (last VELOCITY_WINDOW_SECONDS) for this fingerprint.
        events_in_window = _record_and_count_velocity(
            normalized.fingerprint, raw_event_id, received_at.timestamp()
        )
        scoring = calculate_criticality(
            event=normalized,
            affected_users=incident.affected_users_count,
            event_count=incident.event_count,
            countries=list(incident.countries or []),
            has_existing_tickets=False,  # Freshdesk check happens async
            events_in_window=events_in_window,
        )

        # ── Anomaly detection: catch silent business degradation ─────────────
        # Product events may carry a metrics payload (failure/pending rates, p95
        # latency, volume series). A tripped anomaly forces an alert and boosts the
        # score even when no single error looks severe.
        anomaly = detect_anomaly(payload.get("metrics") if isinstance(payload, dict) else None)
        if anomaly.is_anomaly:
            scoring.total_score += anomaly.severity_boost
            scoring.severity = _score_to_severity(scoring.total_score)
            scoring.breakdown["anomaly"] = anomaly.severity_boost
            logger.info(f"[WORKER] ⚠️ Anomaly detected ({anomaly.kind}): {anomaly.detail}")

        await incident_service.update_incident_score(db, incident, scoring)

        logger.info(
            f"[WORKER] Score: {scoring.total_score} | Severity: {scoring.severity} | "
            f"Breakdown: {scoring.breakdown}"
        )

        # ── Step 5: Decide whether to alert ─────────────────────────────────
        # Critical business actions (e.g. /withdraw) alert at a lower bar so a
        # low-volume but high-impact financial issue still beats support.
        threshold = (
            settings.CRITICAL_BUSINESS_ACTION_THRESHOLD if scoring.is_critical_path
            else settings.INCIDENT_ALERT_THRESHOLD
        )
        already_alerted = (
            incident.agent_alert_timestamp is not None
            or incident.status in ("alerted", "enriched", "matched_to_freshdesk")
        )
        should_alert = (
            (scoring.total_score >= threshold or anomaly.is_anomaly)
            and not already_alerted
        )

        # Cross-worker guard: during a burst, several events for the same
        # fingerprint can pass the check before the first commits a delivered alert.
        # Only the lock holder proceeds to alert; the rest skip (no duplicate Slack).
        if should_alert and not _acquire_alert_lock(normalized.fingerprint):
            logger.info(f"[WORKER] Alert lock held for {normalized.fingerprint} — skipping duplicate alert")
            should_alert = False

        if not should_alert:
            logger.info(f"[WORKER] Below threshold or already alerted — skipping alert")
            raw_event.processed = True
            await db.commit()
            return

        # ── Step 6+7: minimal immediate alert → delivered timestamp → COMMIT ─
        # The alert is sent with NO LLM. If anything fails before the commit we
        # release the lock so a Celery retry can re-attempt (otherwise the lock
        # would block re-alerting for its whole TTL).
        try:
            result = await alerting.deliver_alert(db, incident, normalized, scoring)
            raw_event.processed = True
            # Commit the delivered (or failed) outcome BEFORE enrichment so a crash
            # mid-enrichment can never lose a real, delivered win.
            await db.commit()
        except Exception:
            _release_alert_lock(normalized.fingerprint)
            raise

        if not result.delivered:
            logger.error(f"[WORKER] ❌ Alert delivery failed for {incident.id} — no timestamp set")
            _release_alert_lock(normalized.fingerprint)
            return

        # ── Step 8: LLM enrichment AFTER delivery (best-effort) → COMMIT ─────
        # Errors here are swallowed inside enrich_incident; the delivered win stands.
        try:
            await alerting.enrich_incident(
                db, incident, normalized, scoring, thread_ts=result.thread_ts,
            )
            await db.commit()
        except Exception as e:
            logger.error(f"[WORKER] Enrichment commit failed for {incident.id}: {e}")
            await db.rollback()

        logger.info(f"[WORKER] ✅ Pipeline complete for incident {incident.id}")
