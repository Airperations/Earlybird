"""
Tests for the fast-path alerting orchestrator — the core winning behavior.

Proves:
  • the Slack alert is sent BEFORE any LLM summary is generated
  • the official benchmark timestamp is set ONLY after confirmed delivery
  • a failed delivery records NO timestamp and moves to notification_failed
  • LLM enrichment runs strictly AFTER delivery and never blocks it
"""

import pytest

from app.incidents import alerting
from app.alerts.slack import AlertDeliveryResult
from tests.conftest import make_normalized, make_scoring, make_incident


@pytest.mark.asyncio
async def test_slack_alert_happens_before_llm_summary(db):
    incident = make_incident()
    db.add(incident)
    await db.flush()

    order = []

    def fake_send_alert(**kwargs):
        order.append("alert")
        return AlertDeliveryResult(delivered=True, message_id="ts1", thread_ts="ts1", attempts=1)

    def fake_generate(context):
        order.append("llm")
        return {"title": "Withdrawal failures", "summary": "s"}

    def fake_followup(**kwargs):
        order.append("followup")
        return AlertDeliveryResult(delivered=True, message_id="ts2")

    await alerting.run_alert_pipeline(
        db, incident, make_normalized(), make_scoring(),
        send_alert=fake_send_alert, generate_summary=fake_generate, send_followup=fake_followup,
    )

    # THE key assertion for the challenge: alert strictly before LLM.
    assert order == ["alert", "llm", "followup"]
    assert order.index("alert") < order.index("llm")


@pytest.mark.asyncio
async def test_official_timestamp_set_only_after_delivery(db):
    incident = make_incident()
    db.add(incident)
    await db.flush()

    seen = {}

    def fake_send_alert(**kwargs):
        # At the moment we attempt delivery, the benchmark timestamp must NOT exist.
        seen["ts_at_send_time"] = incident.agent_alert_timestamp
        return AlertDeliveryResult(delivered=True, message_id="ts1", thread_ts="ts1", attempts=1)

    await alerting.deliver_alert(
        db, incident, make_normalized(), make_scoring(), send_alert=fake_send_alert,
    )

    assert seen["ts_at_send_time"] is None                 # not set before delivery
    assert incident.agent_alert_timestamp is not None       # set after delivery
    # The benchmark field mirrors the real delivery timestamp exactly.
    assert incident.agent_alert_timestamp == incident.notification_delivered_at
    assert incident.status == "alerted"
    assert incident.notification_status == "delivered"


@pytest.mark.asyncio
async def test_failed_delivery_records_no_timestamp(db):
    incident = make_incident()
    db.add(incident)
    await db.flush()

    def fake_fail(**kwargs):
        return AlertDeliveryResult(delivered=False, attempts=3, error="slack down")

    result = await alerting.deliver_alert(
        db, incident, make_normalized(), make_scoring(), send_alert=fake_fail,
    )

    assert result.delivered is False
    assert incident.agent_alert_timestamp is None           # NO fake win
    assert incident.notification_delivered_at is None
    assert incident.status == "notification_failed"
    assert incident.notification_status == "failed"
    assert incident.notification_attempts == 3


@pytest.mark.asyncio
async def test_enrichment_skipped_when_delivery_fails(db):
    incident = make_incident()
    db.add(incident)
    await db.flush()

    llm_calls = []

    def fake_fail(**kwargs):
        return AlertDeliveryResult(delivered=False, attempts=3, error="x")

    def fake_generate(context):
        llm_calls.append(1)
        return {"title": "t"}

    await alerting.run_alert_pipeline(
        db, incident, make_normalized(), make_scoring(),
        send_alert=fake_fail, generate_summary=fake_generate,
        send_followup=lambda **k: AlertDeliveryResult(delivered=True),
    )

    # No delivery → no enrichment work, no win.
    assert llm_calls == []
    assert incident.agent_alert_timestamp is None


@pytest.mark.asyncio
async def test_alert_still_counts_when_llm_fails(db):
    incident = make_incident()
    db.add(incident)
    await db.flush()

    def fake_send_alert(**kwargs):
        return AlertDeliveryResult(delivered=True, message_id="ts1", thread_ts="ts1", attempts=1)

    def broken_llm(context):
        raise RuntimeError("Anthropic timeout")

    result = await alerting.run_alert_pipeline(
        db, incident, make_normalized(), make_scoring(),
        send_alert=fake_send_alert, generate_summary=broken_llm,
        send_followup=lambda **k: AlertDeliveryResult(delivered=True),
    )

    # LLM blew up, but the delivered alert (and its timestamp) stands.
    assert result.delivered is True
    assert incident.agent_alert_timestamp is not None
    assert incident.status == "alerted"   # not enriched (no summary), but still alerted


@pytest.mark.asyncio
async def test_enrichment_runs_after_delivery_timestamp(db):
    incident = make_incident()
    db.add(incident)
    await db.flush()

    def fake_send_alert(**kwargs):
        return AlertDeliveryResult(delivered=True, message_id="ts1", thread_ts="ts1", attempts=1)

    await alerting.run_alert_pipeline(
        db, incident, make_normalized(), make_scoring(),
        send_alert=fake_send_alert,
        generate_summary=lambda c: {"title": "Withdrawal failures", "summary": "s"},
        send_followup=lambda **k: AlertDeliveryResult(delivered=True),
    )

    assert incident.enriched_at is not None
    assert incident.notification_delivered_at is not None
    assert incident.enriched_at >= incident.notification_delivered_at
    assert incident.status == "enriched"
    assert incident.title == "Withdrawal failures"
