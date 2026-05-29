"""
Earlybird — Anomaly Detection

Catches *business degradation* before any single error looks alarming and before
users complain. A 502 storm trips the error scorer; a quiet drift — withdrawals
that used to succeed now silently pending, p95 latency doubling, a volume spike
in a financial action — does not. These detectors close that gap.

All detectors are pure functions returning an AnomalyResult, so they are trivially
testable and free of I/O. The worker feeds them counts/rates it already tracks
(Redis sliding windows, product-event metric payloads).

Thresholds come from app.config.settings but are injectable for testing.
"""

from dataclasses import dataclass, field
from statistics import mean, pstdev
from typing import List, Optional

from app.config import settings


@dataclass
class AnomalyResult:
    is_anomaly: bool
    kind: Optional[str] = None      # volume_spike | failure_rate | pending_rate | latency_regression
    z_score: Optional[float] = None
    severity_boost: int = 0         # points to add to the incident score when wired in
    detail: dict = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.is_anomaly


def detect_volume_spike(
    current: float,
    baseline: List[float],
    *,
    z_threshold: Optional[float] = None,
    min_samples: Optional[int] = None,
) -> AnomalyResult:
    """
    Flag a statistically significant spike in event volume vs a baseline series.

    Uses a population z-score: z = (current - mean) / stdev. Requires at least
    `min_samples` baseline points so a couple of noisy readings can't manufacture
    an anomaly. A flat baseline (stdev == 0) only trips if current exceeds it.
    """
    z_threshold = z_threshold if z_threshold is not None else settings.ANOMALY_Z_SCORE_THRESHOLD
    min_samples = min_samples if min_samples is not None else settings.ANOMALY_MIN_SAMPLE_SIZE

    if len(baseline) < min_samples:
        return AnomalyResult(False, detail={"reason": "insufficient_baseline", "n": len(baseline)})

    mu = mean(baseline)
    sigma = pstdev(baseline)

    if sigma == 0:
        # No historical variance: any value above the flat baseline is a spike.
        spiked = current > mu
        return AnomalyResult(
            is_anomaly=spiked,
            kind="volume_spike" if spiked else None,
            z_score=float("inf") if spiked else 0.0,
            severity_boost=40 if spiked else 0,
            detail={"mean": mu, "stdev": 0.0, "current": current},
        )

    z = (current - mu) / sigma
    spiked = z >= z_threshold
    return AnomalyResult(
        is_anomaly=spiked,
        kind="volume_spike" if spiked else None,
        z_score=round(z, 3),
        severity_boost=min(60, int(z * 10)) if spiked else 0,
        detail={"mean": round(mu, 3), "stdev": round(sigma, 3), "current": current, "z": round(z, 3)},
    )


def detect_failure_rate(
    failures: int,
    total: int,
    *,
    threshold: Optional[float] = None,
    min_samples: Optional[int] = None,
    critical: bool = False,
) -> AnomalyResult:
    """
    Flag an elevated failure rate for an action (e.g. withdrawals failing).

    For critical business actions a smaller absolute sample is meaningful (money
    is involved), so the minimum sample size drops to ANOMALY_CRITICAL_MIN_SAMPLE_SIZE.
    """
    threshold = threshold if threshold is not None else settings.ANOMALY_FAILURE_RATE_THRESHOLD
    if min_samples is None:
        min_samples = (
            settings.ANOMALY_CRITICAL_MIN_SAMPLE_SIZE if critical
            else settings.ANOMALY_MIN_SAMPLE_SIZE
        )

    if total < min_samples:
        return AnomalyResult(False, detail={"reason": "insufficient_sample", "total": total})

    rate = failures / total if total else 0.0
    tripped = rate >= threshold
    return AnomalyResult(
        is_anomaly=tripped,
        kind="failure_rate" if tripped else None,
        severity_boost=50 if tripped else 0,
        detail={"failures": failures, "total": total, "rate": round(rate, 3), "threshold": threshold},
    )


def detect_pending_rate(
    pending: int,
    total: int,
    *,
    threshold: Optional[float] = None,
    min_samples: Optional[int] = None,
) -> AnomalyResult:
    """
    Flag transactions piling up in a pending/stuck state — a classic silent
    degradation (the request 'succeeds' but never settles) that users feel long
    before it surfaces as an error.
    """
    threshold = threshold if threshold is not None else settings.ANOMALY_PENDING_RATE_THRESHOLD
    min_samples = min_samples if min_samples is not None else settings.ANOMALY_MIN_SAMPLE_SIZE

    if total < min_samples:
        return AnomalyResult(False, detail={"reason": "insufficient_sample", "total": total})

    rate = pending / total if total else 0.0
    tripped = rate >= threshold
    return AnomalyResult(
        is_anomaly=tripped,
        kind="pending_rate" if tripped else None,
        severity_boost=40 if tripped else 0,
        detail={"pending": pending, "total": total, "rate": round(rate, 3), "threshold": threshold},
    )


def detect_latency_regression(
    current_p95: float,
    baseline_p95: float,
    *,
    factor: Optional[float] = None,
) -> AnomalyResult:
    """Flag a latency regression where current p95 ≥ factor × baseline p95."""
    factor = factor if factor is not None else settings.ANOMALY_LATENCY_REGRESSION_FACTOR
    if baseline_p95 <= 0:
        return AnomalyResult(False, detail={"reason": "no_baseline"})

    ratio = current_p95 / baseline_p95
    tripped = ratio >= factor
    return AnomalyResult(
        is_anomaly=tripped,
        kind="latency_regression" if tripped else None,
        severity_boost=30 if tripped else 0,
        detail={
            "current_p95": current_p95,
            "baseline_p95": baseline_p95,
            "ratio": round(ratio, 3),
            "factor": factor,
        },
    )


def detect_anomaly(metrics: Optional[dict]) -> AnomalyResult:
    """
    Dispatcher over a product-event metrics payload. Returns the first (highest
    priority) anomaly found. Recognised optional keys:

      failure_count + total_count [+ critical]   → failure_rate
      pending_count + total_count                → pending_rate
      latency_p95 + baseline_p95                 → latency_regression
      current_volume + baseline_volume (list)    → volume_spike

    Returns a negative AnomalyResult when nothing applies, so callers can treat
    the result as a boolean.
    """
    if not metrics:
        return AnomalyResult(False, detail={"reason": "no_metrics"})

    total = metrics.get("total_count")
    critical = bool(metrics.get("critical"))

    if metrics.get("failure_count") is not None and total is not None:
        r = detect_failure_rate(metrics["failure_count"], total, critical=critical)
        if r:
            return r

    if metrics.get("pending_count") is not None and total is not None:
        r = detect_pending_rate(metrics["pending_count"], total)
        if r:
            return r

    if metrics.get("latency_p95") is not None and metrics.get("baseline_p95") is not None:
        r = detect_latency_regression(metrics["latency_p95"], metrics["baseline_p95"])
        if r:
            return r

    if metrics.get("current_volume") is not None and isinstance(metrics.get("baseline_volume"), list):
        r = detect_volume_spike(metrics["current_volume"], metrics["baseline_volume"])
        if r:
            return r

    return AnomalyResult(False, detail={"reason": "no_anomaly"})
