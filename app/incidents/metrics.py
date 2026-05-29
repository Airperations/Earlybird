"""
Earlybird — Self-built metrics & rolling-baseline anomaly detection.

The agent does not depend on the producer to pre-compute rates. It aggregates
every event into per-minute `MetricBucket` cells across dimensions
(global / country / provider / platform / payment_method), then compares a recent
window to the preceding baseline window *per dimension*. This is what lets it say:

    "withdrawal success rate in MX dropped 97% → 71%"
    "withdrawal pending rate in provider=stripe is 5σ above baseline"
    "deposit p95 latency is 3.8× its own baseline"

The statistical decisions reuse the pure detectors in app.incidents.anomaly, so
the math is shared and unit-tested in isolation; this module only builds the
series and the DB I/O around them.
"""

import uuid
import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import MetricBucket
from app.normalizers.base import NormalizedEventSchema
from app.taxonomy import base_action, CRITICAL_ACTIONS
from app.incidents.anomaly import (
    AnomalyResult,
    detect_volume_spike,
    detect_failure_rate,
    detect_pending_rate,
    detect_latency_regression,
)

logger = logging.getLogger(__name__)

# Dimensions tracked alongside the always-present "global" cell. The tuple value
# is the NormalizedEventSchema attribute that carries each dimension's value.
DIMENSIONS = (
    ("country", "country"),
    ("provider", "provider"),
    ("platform", "platform"),
    ("payment_method", "payment_method"),
)


def floor_minute(dt: datetime) -> datetime:
    """Truncate a timestamp to the start of its minute (UTC)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.replace(second=0, microsecond=0)


def as_aware(dt: Optional[datetime]) -> Optional[datetime]:
    """
    Coerce a datetime to UTC-aware. SQLite drops tzinfo on round-trip while
    Postgres preserves it; normalizing here keeps window comparisons backend-safe.
    """
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def classify_event_outcome(normalized: NormalizedEventSchema, payload: Optional[dict]) -> str:
    """
    Decide whether an event was a success / failure / pending for rate metrics.

    Priority: an explicit producer signal (`outcome`/`status`) wins; otherwise we
    infer from http_status / exception. Error-only sources (Sentry/Datadog) with
    no explicit signal count as failures — that's why they reached us.
    """
    raw = None
    if isinstance(payload, dict):
        # `alert_status` is the Datadog monitor transition (Triggered/Recovered/…).
        raw = payload.get("outcome") or payload.get("status") or payload.get("alert_status")
    if raw:
        r = str(raw).lower()
        if r in ("success", "ok", "succeeded", "completed", "settled", "approved", "recovered", "resolved"):
            return "success"
        if r in ("pending", "processing", "stuck", "queued", "in_progress", "warn", "warning", "no data"):
            return "pending"
        if r in ("failed", "failure", "error", "declined", "rejected", "timeout", "triggered", "alert"):
            return "failure"

    if normalized.exception_type:
        return "failure"
    if normalized.http_status is not None:
        return "failure" if normalized.http_status >= 400 else "success"
    return "failure"


def _dimension_cells(normalized: NormalizedEventSchema):
    """The (dimension, value) cells this event contributes to."""
    cells = [("global", "ALL")]
    for dim, attr in DIMENSIONS:
        val = getattr(normalized, attr, None)
        if val:
            cells.append((dim, str(val)))
    return cells


def _int(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _aggregate_counts(payload: Optional[dict]) -> Optional[dict]:
    """
    If the payload carries pre-aggregated metric counts (e.g. a Datadog monitor /
    custom webhook ``metrics`` block), return them as a counts delta so a single
    event can fold N transactions into the buckets. Returns None for an ordinary
    single event (caller falls back to a +1 increment).

    Recognised keys: total_count, success_count, failure_count, pending_count.
    """
    metrics = payload.get("metrics") if isinstance(payload, dict) else None
    if not isinstance(metrics, dict):
        return None
    keys = ("total_count", "success_count", "failure_count", "pending_count")
    if not any(metrics.get(k) is not None for k in keys):
        return None
    success = _int(metrics.get("success_count")) or 0
    failure = _int(metrics.get("failure_count")) or 0
    pending = _int(metrics.get("pending_count")) or 0
    total = _int(metrics.get("total_count"))
    if total is None:
        total = success + failure + pending
    return {"total": total, "success": success, "failure": failure, "pending": pending}


async def _upsert_bucket(
    db: AsyncSession, minute: datetime, action: str, dimension: str,
    value: str, counts: dict, latency_ms: Optional[float],
) -> None:
    """Increment (or create) one metric cell. Concurrency-safe via a savepoint."""
    bucket = (await db.execute(
        select(MetricBucket)
        .where(MetricBucket.bucket_start == minute)
        .where(MetricBucket.business_action == action)
        .where(MetricBucket.dimension == dimension)
        .where(MetricBucket.dimension_value == value)
    )).scalar_one_or_none()

    if bucket is None:
        bucket = MetricBucket(
            id=uuid.uuid4(), bucket_start=minute, business_action=action,
            dimension=dimension, dimension_value=value,
            total_count=0, success_count=0, failure_count=0, pending_count=0,
            latency_count=0, latency_sum_ms=0.0, latency_max_ms=0.0,
        )
        try:
            async with db.begin_nested():
                db.add(bucket)
                await db.flush()
        except IntegrityError:
            # Another worker created the cell first — re-fetch and increment it.
            await db.rollback()
            bucket = (await db.execute(
                select(MetricBucket)
                .where(MetricBucket.bucket_start == minute)
                .where(MetricBucket.business_action == action)
                .where(MetricBucket.dimension == dimension)
                .where(MetricBucket.dimension_value == value)
            )).scalar_one()

    bucket.total_count += counts["total"]
    bucket.success_count += counts["success"]
    bucket.pending_count += counts["pending"]
    bucket.failure_count += counts["failure"]
    if latency_ms is not None:
        try:
            lm = float(latency_ms)
            bucket.latency_count += 1
            bucket.latency_sum_ms += lm
            bucket.latency_max_ms = max(bucket.latency_max_ms, lm)
        except (TypeError, ValueError):
            pass
    bucket.updated_at = datetime.now(timezone.utc)


async def record_event_metrics(
    db: AsyncSession,
    normalized: NormalizedEventSchema,
    payload: Optional[dict] = None,
    *,
    now: Optional[datetime] = None,
) -> Optional[str]:
    """
    Fold one event into the per-minute metric buckets across all its dimensions.
    Returns the classified outcome, or None if the event has no business action
    (we only build baselines for known money/platform flows).
    """
    action = base_action(normalized.business_action)
    if not action:
        return None
    now = now or datetime.now(timezone.utc)
    minute = floor_minute(now)
    outcome = classify_event_outcome(normalized, payload)

    # A Datadog monitor / custom webhook can carry pre-aggregated counts; fold all
    # of them in. An ordinary single event is just a +1 in its classified bucket.
    counts = _aggregate_counts(payload)
    if counts is None:
        counts = {
            "total": 1,
            "success": 1 if outcome == "success" else 0,
            "pending": 1 if outcome == "pending" else 0,
            "failure": 1 if outcome == "failure" else 0,
        }

    latency_ms = None
    if isinstance(payload, dict):
        latency_ms = payload.get("latency_ms")
        if latency_ms is None and isinstance(payload.get("metrics"), dict):
            # Datadog metric blocks report p95 latency; use it as one latency sample.
            latency_ms = payload["metrics"].get("p95_latency_ms")

    for dimension, value in _dimension_cells(normalized):
        await _upsert_bucket(db, minute, action, dimension, value, counts, latency_ms)
    return outcome


# ─── Rolling-baseline analysis ────────────────────────────────────────────────

@dataclass
class _Window:
    total: int = 0
    success: int = 0
    failure: int = 0
    pending: int = 0
    latency_count: int = 0
    latency_sum: float = 0.0
    latency_max: float = 0.0

    @property
    def success_rate(self) -> Optional[float]:
        return (self.success / self.total) if self.total else None

    @property
    def mean_latency(self) -> Optional[float]:
        return (self.latency_sum / self.latency_count) if self.latency_count else None


def _fold(buckets: List[MetricBucket]) -> _Window:
    w = _Window()
    for b in buckets:
        w.total += b.total_count
        w.success += b.success_count
        w.failure += b.failure_count
        w.pending += b.pending_count
        w.latency_count += b.latency_count
        w.latency_sum += b.latency_sum_ms
        w.latency_max = max(w.latency_max, b.latency_max_ms)
    return w


def analyze_series(
    current_buckets: List[MetricBucket],
    baseline_buckets: List[MetricBucket],
    *,
    action: str,
    dimension: str,
    value: str,
    critical: bool = False,
) -> AnomalyResult:
    """
    Pure comparison of a current window vs its baseline for one metric cell.
    Returns the highest-severity anomaly among: success-rate drop, failure rate,
    pending rate, latency regression, volume spike. Detail always names the
    action/dimension/value so an alert reads "withdrawal · country=MX".
    """
    cur = _fold(current_buckets)
    base = _fold(baseline_buckets)
    min_total = (settings.ANOMALY_BASELINE_CRITICAL_MIN_TOTAL if critical
                 else settings.ANOMALY_BASELINE_MIN_TOTAL)

    where = {"action": action, "dimension": dimension, "value": value}
    candidates: List[AnomalyResult] = []

    # 1) Success-rate drop vs the cell's own baseline.
    if cur.total and base.total >= min_total:
        if cur.success_rate is not None and base.success_rate is not None:
            drop = base.success_rate - cur.success_rate
            if drop >= settings.ANOMALY_SUCCESS_RATE_DROP:
                candidates.append(AnomalyResult(
                    is_anomaly=True, kind="success_rate_drop",
                    severity_boost=min(60, int(drop * 100)),
                    detail={**where, "baseline_success_rate": round(base.success_rate, 3),
                            "current_success_rate": round(cur.success_rate, 3),
                            "drop": round(drop, 3)},
                ))

    # 2) Elevated failure rate in the current window.
    fr = detect_failure_rate(cur.failure, cur.total, critical=critical)
    if fr:
        fr.detail.update(where)
        candidates.append(fr)

    # 3) Pending/stuck pile-up in the current window.
    pr = detect_pending_rate(cur.pending, cur.total)
    if pr:
        pr.detail.update(where)
        candidates.append(pr)

    # 4) Latency regression vs baseline mean.
    if cur.mean_latency is not None and base.mean_latency:
        lr = detect_latency_regression(cur.mean_latency, base.mean_latency)
        if lr:
            lr.detail.update(where)
            candidates.append(lr)

    # 5) Volume spike vs the baseline per-minute series.
    baseline_series = [b.total_count for b in baseline_buckets]
    vs = detect_volume_spike(float(cur.total), baseline_series)
    if vs:
        vs.detail.update(where)
        candidates.append(vs)

    if not candidates:
        return AnomalyResult(False, detail={**where, "reason": "no_anomaly"})
    return max(candidates, key=lambda r: r.severity_boost)


async def detect_baseline_anomaly(
    db: AsyncSession,
    normalized: NormalizedEventSchema,
    *,
    now: Optional[datetime] = None,
    current_minutes: Optional[int] = None,
    baseline_minutes: Optional[int] = None,
) -> AnomalyResult:
    """
    Build current + baseline windows from MetricBuckets for this event's action,
    across every dimension it touches, and return the strongest anomaly found.
    """
    action = base_action(normalized.business_action)
    if not action:
        return AnomalyResult(False, detail={"reason": "no_business_action"})

    now = now or datetime.now(timezone.utc)
    current_minutes = current_minutes or settings.ANOMALY_CURRENT_WINDOW_MINUTES
    baseline_minutes = baseline_minutes or settings.ANOMALY_BASELINE_WINDOW_MINUTES
    critical = action in CRITICAL_ACTIONS

    current_start = floor_minute(now) - timedelta(minutes=current_minutes - 1)
    baseline_start = current_start - timedelta(minutes=baseline_minutes)

    best = AnomalyResult(False)
    for dimension, value in _dimension_cells(normalized):
        rows = (await db.execute(
            select(MetricBucket)
            .where(MetricBucket.business_action == action)
            .where(MetricBucket.dimension == dimension)
            .where(MetricBucket.dimension_value == value)
            .where(MetricBucket.bucket_start >= baseline_start)
            .order_by(MetricBucket.bucket_start.asc())
        )).scalars().all()

        current = [b for b in rows if as_aware(b.bucket_start) >= current_start]
        baseline = [b for b in rows if as_aware(b.bucket_start) < current_start]
        if not current:
            continue

        result = analyze_series(
            current, baseline, action=action, dimension=dimension,
            value=value, critical=critical,
        )
        if result.is_anomaly and result.severity_boost > best.severity_boost:
            best = result

    return best
