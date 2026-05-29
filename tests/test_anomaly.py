"""Tests for anomaly detection — catching silent business degradation."""

from app.incidents import anomaly


def test_volume_spike_detected_with_zscore():
    baseline = [10, 11, 9, 10, 12, 8, 10, 11, 9, 10] * 3  # 30 samples, mean ~10
    r = anomaly.detect_volume_spike(80, baseline, z_threshold=3.0, min_samples=20)
    assert r.is_anomaly
    assert r.kind == "volume_spike"
    assert r.z_score >= 3.0


def test_volume_spike_requires_min_sample_size():
    r = anomaly.detect_volume_spike(80, [10, 12, 9], z_threshold=3.0, min_samples=20)
    assert not r.is_anomaly
    assert r.detail["reason"] == "insufficient_baseline"


def test_volume_normal_not_flagged():
    baseline = [10, 11, 9, 10, 12, 8, 10, 11, 9, 10] * 3
    r = anomaly.detect_volume_spike(11, baseline, z_threshold=3.0, min_samples=20)
    assert not r.is_anomaly


def test_failure_rate_anomaly():
    r = anomaly.detect_failure_rate(45, 100, threshold=0.30, min_samples=20)
    assert r.is_anomaly
    assert r.kind == "failure_rate"
    assert r.severity_boost > 0


def test_failure_rate_below_threshold():
    r = anomaly.detect_failure_rate(10, 100, threshold=0.30, min_samples=20)
    assert not r.is_anomaly


def test_critical_action_small_sample_still_counts():
    # Only 4 withdrawals, 3 failed — too small for the normal min, but money is
    # involved so the critical path lowers the bar.
    r = anomaly.detect_failure_rate(3, 4, threshold=0.30, critical=True, min_samples=3)
    assert r.is_anomaly


def test_pending_rate_anomaly():
    r = anomaly.detect_pending_rate(60, 100, threshold=0.40, min_samples=20)
    assert r.is_anomaly
    assert r.kind == "pending_rate"


def test_latency_regression():
    r = anomaly.detect_latency_regression(900, 400, factor=2.0)
    assert r.is_anomaly
    assert r.kind == "latency_regression"


def test_latency_no_regression():
    r = anomaly.detect_latency_regression(450, 400, factor=2.0)
    assert not r.is_anomaly


def test_dispatcher_failure_rate():
    r = anomaly.detect_anomaly({"failure_count": 50, "total_count": 100, "critical": True})
    assert r.is_anomaly
    assert r.kind == "failure_rate"


def test_dispatcher_no_metrics():
    assert not anomaly.detect_anomaly(None)
    assert not anomaly.detect_anomaly({})


def test_anomaly_result_is_falsy_when_negative():
    r = anomaly.detect_anomaly({})
    assert bool(r) is False
