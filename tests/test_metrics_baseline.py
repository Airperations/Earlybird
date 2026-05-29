"""
Tests for the self-built MetricBucket aggregation + rolling-baseline anomaly
detection (the agent builds its own per-minute, per-dimension metrics and
compares a recent window to its own baseline).
"""

import uuid
from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy import select

from app.incidents import metrics
from app.incidents.metrics import (
    record_event_metrics, detect_baseline_anomaly, classify_event_outcome,
    analyze_series, floor_minute, as_aware,
)
from app.models import MetricBucket
from tests.conftest import make_normalized


# ── outcome classification ────────────────────────────────────────────────────

def test_classify_outcome_explicit_and_inferred():
    n = make_normalized(http_status=200, exception_type=None)
    assert classify_event_outcome(n, {"outcome": "success"}) == "success"
    assert classify_event_outcome(n, {"status": "pending"}) == "pending"
    assert classify_event_outcome(n, {"outcome": "declined"}) == "failure"
    # Inference when no explicit signal:
    assert classify_event_outcome(make_normalized(http_status=200, exception_type=None), None) == "success"
    assert classify_event_outcome(make_normalized(http_status=502, exception_type=None), None) == "failure"


# ── aggregation across dimensions ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_record_event_metrics_fans_out_dimensions(db):
    now = datetime(2026, 5, 29, 12, 30, 15, tzinfo=timezone.utc)
    n = make_normalized(business_action="withdrawal", country="MX", platform="ios",
                        provider="stripe", payment_method="card")
    outcome = await record_event_metrics(db, n, {"outcome": "success", "latency_ms": 120}, now=now)
    await db.flush()
    assert outcome == "success"

    rows = (await db.execute(select(MetricBucket))).scalars().all()
    cells = {(r.dimension, r.dimension_value) for r in rows}
    assert ("global", "ALL") in cells
    assert ("country", "MX") in cells
    assert ("provider", "stripe") in cells
    assert ("platform", "ios") in cells
    assert ("payment_method", "card") in cells
    for r in rows:
        assert as_aware(r.bucket_start) == floor_minute(now)
        assert r.business_action == "withdrawal"
        assert r.success_count == 1 and r.total_count == 1
        assert r.latency_count == 1 and r.latency_sum_ms == 120.0


@pytest.mark.asyncio
async def test_record_event_metrics_skips_unknown_action(db):
    n = make_normalized(business_action=None, endpoint="/profile/avatar")
    assert await record_event_metrics(db, n, None, now=datetime.now(timezone.utc)) is None
    assert (await db.execute(select(MetricBucket))).scalars().all() == []


@pytest.mark.asyncio
async def test_same_minute_increments_one_bucket(db):
    now = datetime(2026, 5, 29, 12, 30, 5, tzinfo=timezone.utc)
    n = make_normalized(business_action="withdrawal", country="MX", platform=None,
                        provider=None, payment_method=None)
    await record_event_metrics(db, n, {"outcome": "success"}, now=now)
    await record_event_metrics(db, n, {"outcome": "failed"}, now=now + timedelta(seconds=20))
    await db.flush()
    mx = (await db.execute(
        select(MetricBucket).where(MetricBucket.dimension == "country").where(MetricBucket.dimension_value == "MX")
    )).scalar_one()
    assert mx.total_count == 2
    assert mx.success_count == 1
    assert mx.failure_count == 1


# ── pure baseline analysis ────────────────────────────────────────────────────

def _bucket(minute, total, success, **kw):
    return MetricBucket(
        id=uuid.uuid4(), bucket_start=minute, business_action="withdrawal",
        dimension="country", dimension_value="MX",
        total_count=total, success_count=success,
        failure_count=kw.get("failure", total - success), pending_count=kw.get("pending", 0),
        latency_count=kw.get("latency_count", 0), latency_sum_ms=kw.get("latency_sum", 0.0),
        latency_max_ms=kw.get("latency_max", 0.0),
    )


def test_analyze_series_success_rate_drop():
    base_min = floor_minute(datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc))
    baseline = [_bucket(base_min + timedelta(minutes=i), 20, 20) for i in range(10)]   # 100%
    current = [_bucket(base_min + timedelta(minutes=20), 20, 14)]                       # 70%
    result = analyze_series(current, baseline, action="withdrawal", dimension="country",
                            value="MX", critical=True)
    assert result.is_anomaly
    # success-rate drop should be the headline (0.30 drop), not just failure rate.
    assert result.detail["dimension"] == "country" and result.detail["value"] == "MX"
    assert result.kind in ("success_rate_drop", "failure_rate")


# ── DB-backed baseline detection ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_detect_baseline_anomaly_success_rate_drop(db):
    now = datetime(2026, 5, 29, 12, 30, 0, tzinfo=timezone.utc)
    floor = floor_minute(now)
    # Healthy baseline (100% success) across 20 earlier minutes.
    for i in range(5, 25):
        db.add(_bucket(floor - timedelta(minutes=i), 20, 20))
    # Degraded current window (70% success) in the last few minutes.
    db.add(_bucket(floor, 20, 14))
    await db.flush()

    n = make_normalized(business_action="withdrawal_failed", country="MX",
                        platform=None, provider=None, payment_method=None)
    result = await detect_baseline_anomaly(db, n, now=now)
    assert result.is_anomaly
    assert result.detail["dimension"] == "country"
    assert result.detail["value"] == "MX"


@pytest.mark.asyncio
async def test_detect_baseline_anomaly_quiet_when_stable(db):
    now = datetime(2026, 5, 29, 12, 30, 0, tzinfo=timezone.utc)
    floor = floor_minute(now)
    for i in range(5, 25):
        db.add(_bucket(floor - timedelta(minutes=i), 20, 20))
    db.add(_bucket(floor, 20, 20))   # still 100%
    await db.flush()

    n = make_normalized(business_action="withdrawal_failed", country="MX",
                        platform=None, provider=None, payment_method=None)
    result = await detect_baseline_anomaly(db, n, now=now)
    assert not result.is_anomaly
