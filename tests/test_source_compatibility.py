"""
Source-compatibility audit tests — Sentry & Datadog.

Proves, against the real pipeline (normalizer → incident service → metric buckets
→ matcher → alerting), that events from Sentry and Datadog:
  • normalize into structured incident metadata,
  • never persist raw user PII,
  • create/update/deduplicate incidents and cross the alert threshold,
  • feed the self-built MetricBucket baselines (Datadog aggregate counts included),
  • match Freshdesk tickets in Spanish (Sentry) and English (Datadog),
  • degrade gracefully on unknown/partial shapes (no crash, safe defaults).

These are deliberately end-to-end against the SQLite test DB so the claims are
demonstrated, not asserted from the README.
"""

import json
import os
import uuid
from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy import select

from app.normalizers.base import normalize, normalize_sentry, normalize_datadog
from app.incidents import service, alerting
from app.incidents.metrics import record_event_metrics, detect_baseline_anomaly
from app.incidents.scoring import calculate_criticality
from app.incidents.anomaly import detect_anomaly
from app.models import NormalizedEvent, Incident, MetricBucket
from app.redaction import hash_identifier
from app.freshdesk import matcher
from app.alerts.slack import AlertDeliveryResult
from tests.conftest import make_scoring


FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _load(name: str) -> dict:
    with open(os.path.join(FIXTURES, name)) as f:
        return json.load(f)


def sentry_payload() -> dict:
    return _load("sentry_issue_payload.json")


def datadog_payload() -> dict:
    return _load("datadog_monitor_payload.json")


# ──────────────────────────────────────────────────────────────────────────────
# 1. Sentry — normalization → structured incident metadata
# ──────────────────────────────────────────────────────────────────────────────

def test_sentry_payload_normalizes_to_incident_metadata():
    n = normalize("sentry", sentry_payload())
    assert n.source == "sentry"
    assert n.service == "payments-api"
    assert n.environment == "production"
    assert n.endpoint == "/api/v1/withdraw/confirm"
    assert n.http_status == 502
    assert n.exception_type == "GatewayTimeout"
    assert n.country == "MX"
    assert n.provider == "stripe"
    assert n.payment_method == "card"
    assert n.platform == "python"
    assert n.release == "payments-api@1.42.0"
    assert n.business_action == "withdrawal_failed"   # derived from endpoint + 502
    assert n.event_timestamp is not None


@pytest.mark.asyncio
async def test_sentry_raw_user_id_is_hashed_not_stored(db):
    """The raw Sentry user id must never reach the NormalizedEvent row or the
    incident's affected-user set — only its salted hash."""
    n = normalize("sentry", sentry_payload())
    raw_user = "user_mx_4821"
    assert n.user_id == raw_user   # held transiently in memory only

    inc = await service.find_or_create_incident(db, n)
    await service.save_normalized_event(db, raw_event_id=None, normalized=n, incident=inc)
    await db.flush()

    rows = (await db.execute(select(NormalizedEvent))).scalars().all()
    assert len(rows) == 1
    stored = rows[0].user_id
    assert stored != raw_user
    assert stored == hash_identifier(raw_user)
    # Incident tracks the hash, never the raw id.
    assert raw_user not in (inc.affected_user_hashes or [])
    assert hash_identifier(raw_user) in inc.affected_user_hashes


@pytest.mark.asyncio
async def test_sentry_payload_can_create_incident(db):
    n = normalize("sentry", sentry_payload())
    inc = await service.find_or_create_incident(db, n)
    await service.save_normalized_event(db, raw_event_id=None, normalized=n, incident=inc)

    scoring = calculate_criticality(
        event=n, affected_users=inc.affected_users_count,
        event_count=inc.event_count, countries=list(inc.countries or []),
    )
    await service.update_incident_score(db, inc, scoring)
    await db.flush()

    # Structured metadata populated from the event (no LLM needed).
    assert inc.service == "payments-api"
    assert inc.business_action == "withdrawal_failed"
    assert inc.primary_country == "MX"
    assert inc.provider == "stripe"
    assert inc.endpoint == "/api/v1/withdraw/confirm"
    assert inc.score > 0 and inc.severity is not None
    # /withdraw is a critical path → alerts at the lower critical bar.
    assert scoring.is_critical_path is True

    # Dedup: a second identical event folds into the same incident.
    inc2 = await service.find_or_create_incident(db, n)
    assert inc2.id == inc.id
    assert inc2.event_count == 2


@pytest.mark.asyncio
async def test_sentry_incident_matches_spanish_freshdesk_ticket(db):
    n = normalize("sentry", sentry_payload())
    inc = await service.find_or_create_incident(db, n)
    inc.agent_alert_timestamp = datetime(2026, 5, 29, 18, 43, 30, tzinfo=timezone.utc)
    await db.flush()

    ticket = {
        "id": "es-1",
        "subject": "No me deja retirar mi dinero",
        "description": "Intenté hacer un retiro con stripe y falló, no me llega el dinero",
        "tags": ["MX"],
        "created_at": "2026-05-29T18:45:00Z",   # ~90s after alert → agent won
    }
    explanation = matcher.explain_match(inc, ticket)
    assert explanation.confidence >= 0.5
    assert "business_action" in explanation.matched_by
    assert "country" in explanation.matched_by
    assert "keyword_match" in explanation.matched_by
    assert explanation.match_reasons.get("keyword_language") in ("es", "mixed")


# ──────────────────────────────────────────────────────────────────────────────
# 2. Datadog — tag-shape coverage (list / dict / string)
# ──────────────────────────────────────────────────────────────────────────────

def _datadog_with_tags(tags):
    p = datadog_payload()
    p["tags"] = tags
    return p


def _assert_datadog_metadata(n):
    assert n.source == "datadog"
    assert n.service == "payments-api"
    assert n.environment == "production"
    assert n.country == "MX"
    assert n.provider == "stripe"
    assert n.payment_method == "card"
    assert n.platform == "ios"


def test_datadog_tags_list_normalize_to_metadata():
    n = normalize("datadog", _datadog_with_tags(
        ["service:payments-api", "env:production", "country:MX",
         "provider:stripe", "payment_method:card", "platform:ios"]
    ))
    _assert_datadog_metadata(n)


def test_datadog_tags_dict_normalize_to_metadata():
    n = normalize("datadog", _datadog_with_tags({
        "service": "payments-api", "env": "production", "country": "MX",
        "provider": "stripe", "payment_method": "card", "platform": "ios",
    }))
    _assert_datadog_metadata(n)


def test_datadog_tags_string_normalize_to_metadata():
    n = normalize("datadog", _datadog_with_tags(
        "service:payments-api,env:production,country:MX,"
        "provider:stripe,payment_method:card,platform:ios"
    ))
    _assert_datadog_metadata(n)


# ──────────────────────────────────────────────────────────────────────────────
# 3. Datadog — aggregate metrics feed MetricBuckets + baselines
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_datadog_metrics_feed_metric_buckets(db):
    payload = datadog_payload()        # metrics: total 200, success 118, failure 78, pending 4
    n = normalize("datadog", payload)
    now = datetime(2026, 5, 29, 18, 43, 0, tzinfo=timezone.utc)

    outcome = await record_event_metrics(db, n, payload, now=now)
    await db.flush()
    assert outcome == "failure"        # alert_status "Triggered" classifies as failure

    rows = (await db.execute(select(MetricBucket))).scalars().all()
    cells = {(r.dimension, r.dimension_value) for r in rows}
    # Dimensions fan out: global + country + provider + platform + payment_method.
    assert ("global", "ALL") in cells
    assert ("country", "MX") in cells
    assert ("provider", "stripe") in cells
    assert ("platform", "ios") in cells
    assert ("payment_method", "card") in cells

    # The AGGREGATE counts (not a single +1) are folded into every cell.
    for r in rows:
        assert r.business_action == "withdrawal"
        assert r.total_count == 200
        assert r.success_count == 118
        assert r.failure_count == 78
        assert r.pending_count == 4
        assert r.latency_count == 1 and r.latency_sum_ms == 4200.0   # p95 sample


@pytest.mark.asyncio
async def test_datadog_metrics_payload_trips_anomaly_detector():
    """A single Datadog payload with failure counts can force an alert via the
    payload anomaly detector (no historical baseline required)."""
    payload = datadog_payload()
    metrics = dict(payload["metrics"])
    metrics["critical"] = True         # money-flow → small samples count
    result = detect_anomaly(metrics)   # 78/200 = 39% failure ≥ 30% threshold
    assert result.is_anomaly is True
    assert result.kind == "failure_rate"


@pytest.mark.asyncio
async def test_datadog_baseline_anomaly_triggers_after_history(db):
    """With prior healthy buckets, a degraded Datadog window trips the self-built
    rolling baseline (success-rate drop), proving baseline integration."""
    now = datetime(2026, 5, 29, 18, 43, 0, tzinfo=timezone.utc)
    from app.incidents.metrics import floor_minute
    floor = floor_minute(now)
    # Healthy baseline: 20 prior minutes at 100% success for withdrawal · MX.
    healthy = datadog_payload()
    healthy_n = normalize("datadog", healthy)
    for i in range(5, 25):
        await record_event_metrics(
            db, healthy_n,
            {"metrics": {"total_count": 50, "success_count": 50, "failure_count": 0}},
            now=floor - timedelta(minutes=i),
        )
    # Degraded current minute from the real fixture (59% success).
    await record_event_metrics(db, healthy_n, healthy, now=now)
    await db.flush()

    result = await detect_baseline_anomaly(db, healthy_n, now=now)
    assert result.is_anomaly is True
    assert result.detail.get("dimension") in ("global", "country", "provider", "platform", "payment_method")


@pytest.mark.asyncio
async def test_datadog_payload_can_create_incident(db):
    payload = datadog_payload()
    n = normalize("datadog", payload)
    inc = await service.find_or_create_incident(db, n)
    await service.save_normalized_event(db, raw_event_id=None, normalized=n, incident=inc)
    await record_event_metrics(db, n, payload, now=datetime(2026, 5, 29, 18, 43, 0, tzinfo=timezone.utc))

    scoring = calculate_criticality(
        event=n, affected_users=inc.affected_users_count,
        event_count=inc.event_count, countries=list(inc.countries or []),
    )
    await service.update_incident_score(db, inc, scoring)
    await db.flush()

    assert inc.service == "payments-api"
    assert inc.business_action == "withdrawal_failed"
    assert inc.primary_country == "MX"
    assert inc.provider == "stripe"
    assert inc.payment_method == "card"
    assert inc.score > 0 and inc.severity is not None
    assert scoring.is_critical_path is True

    # Dedup across redeliveries.
    inc2 = await service.find_or_create_incident(db, n)
    assert inc2.id == inc.id
    assert inc2.event_count == 2


@pytest.mark.asyncio
async def test_datadog_incident_matches_english_freshdesk_ticket(db):
    n = normalize("datadog", datadog_payload())
    inc = await service.find_or_create_incident(db, n)
    inc.agent_alert_timestamp = datetime(2026, 5, 29, 18, 43, 30, tzinfo=timezone.utc)
    await db.flush()

    ticket = {
        "id": "en-1",
        "subject": "Cannot complete my withdrawal",
        "description": "I tried to withdraw money through stripe and it failed, funds not received",
        "tags": ["MX"],
        "created_at": "2026-05-29T18:46:00Z",   # 150s after alert → agent won
    }
    explanation = matcher.explain_match(inc, ticket)
    assert explanation.confidence >= 0.5
    assert "business_action" in explanation.matched_by
    assert "country" in explanation.matched_by
    assert explanation.match_reasons.get("keyword_language") in ("en", "mixed")


# ──────────────────────────────────────────────────────────────────────────────
# 4. Alerting path — threshold crossing, alert-before-LLM, win only on delivery
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("fixture", ["sentry_issue_payload.json", "datadog_monitor_payload.json"])
async def test_source_incident_alerts_before_llm_and_wins_only_on_delivery(db, fixture):
    n = normalize("sentry" if "sentry" in fixture else "datadog", _load(fixture))
    inc = await service.find_or_create_incident(db, n)
    await db.flush()

    order = []

    def fake_alert(**kwargs):
        order.append("alert")
        # The benchmark timestamp must not be set at delivery-attempt time.
        assert inc.agent_alert_timestamp is None
        return AlertDeliveryResult(delivered=True, message_id="ts1", thread_ts="ts1", attempts=1)

    def fake_llm(context):
        order.append("llm")
        return {"title": "Withdrawal failures", "summary": "s"}

    await alerting.run_alert_pipeline(
        db, inc, n, make_scoring(),
        send_alert=fake_alert, generate_summary=fake_llm,
        send_followup=lambda **k: AlertDeliveryResult(delivered=True),
    )

    assert order[0] == "alert" and "llm" in order
    assert order.index("alert") < order.index("llm")     # alert strictly before LLM
    assert inc.agent_alert_timestamp is not None          # set only after delivery
    assert inc.agent_alert_timestamp == inc.notification_delivered_at


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture", ["sentry_issue_payload.json", "datadog_monitor_payload.json"])
async def test_source_failed_delivery_records_no_win(db, fixture):
    n = normalize("sentry" if "sentry" in fixture else "datadog", _load(fixture))
    inc = await service.find_or_create_incident(db, n)
    await db.flush()

    result = await alerting.deliver_alert(
        db, inc, n, make_scoring(),
        send_alert=lambda **k: AlertDeliveryResult(delivered=False, attempts=3, error="down"),
    )
    assert result.delivered is False
    assert inc.agent_alert_timestamp is None      # no fake win
    assert inc.status == "notification_failed"


# ──────────────────────────────────────────────────────────────────────────────
# 5. Graceful degradation on unknown / partial shapes (no 500s)
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("payload", [
    {},
    {"event": "not-a-dict"},
    {"event": {"tags": "weird-shape"}},
    {"event": {"exception": "broken", "user": 12345}},
    {"foo": "bar", "level": "error"},
])
def test_unknown_sentry_shape_falls_back_gracefully(payload):
    n = normalize_sentry(payload)
    # Safe defaults, never a crash.
    assert n.source == "sentry"
    assert n.service == "unknown"
    assert n.environment == "production"
    assert n.endpoint == ""
    assert n.http_status is None
    assert n.business_action is None     # unknown endpoint → no misleading label


@pytest.mark.parametrize("payload", [
    {},
    {"tags": 99},
    {"metrics": "not-a-dict"},
    {"alert_type": None, "tags": ["malformed-no-colon"]},
    {"tags": [{"unexpected": "object"}]},
])
def test_unknown_datadog_shape_falls_back_gracefully(payload):
    n = normalize_datadog(payload)
    assert n.source == "datadog"
    assert n.service == "unknown"
    assert n.environment == "production"
    assert n.endpoint == ""
    assert n.business_action is None


@pytest.mark.asyncio
async def test_unknown_shape_metrics_recording_is_noop_without_business_action(db):
    """An event with no recognized business action must not create metric buckets."""
    n = normalize_datadog({"tags": ["service:misc"], "alert_type": "error"})
    out = await record_event_metrics(db, n, {"tags": ["service:misc"]},
                                     now=datetime(2026, 5, 29, 18, 43, 0, tzinfo=timezone.utc))
    await db.flush()
    assert out is None
    assert (await db.execute(select(MetricBucket))).scalars().all() == []
