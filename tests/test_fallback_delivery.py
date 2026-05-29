"""
Tests for multichannel fallback delivery: Slack → PagerDuty → email. The FIRST
channel that delivers locks the benchmark timestamp; if all fail, no timestamp.
"""

import pytest

from app.incidents import alerting
from app.alerts.slack import AlertDeliveryResult
from app.alerts import channels
from app.config import settings
from tests.conftest import make_incident, make_normalized, make_scoring


def _fail(channel, attempts=3):
    return lambda **kw: AlertDeliveryResult(delivered=False, channel=channel, attempts=attempts, error=f"{channel} down")


@pytest.mark.asyncio
async def test_pagerduty_used_when_slack_fails(db):
    incident = make_incident()
    db.add(incident)
    await db.flush()

    def slack_fail(**kw):
        return AlertDeliveryResult(delivered=False, channel="slack", attempts=3, error="slack down")

    def pd_ok(**kw):
        return AlertDeliveryResult(delivered=True, channel="pagerduty", message_id="pd1", attempts=1)

    result = await alerting.deliver_alert(
        db, incident, make_normalized(), make_scoring(),
        send_alert=slack_fail, send_pagerduty=pd_ok, send_email=_fail("email"),
    )

    assert result.delivered is True
    assert result.channel == "pagerduty"
    assert incident.agent_alert_timestamp is not None         # first delivered channel locked it
    assert incident.notification_channel == "pagerduty"
    assert incident.notification_status == "delivered"
    assert incident.status == "alerted"


@pytest.mark.asyncio
async def test_email_used_when_slack_and_pagerduty_fail(db):
    incident = make_incident()
    db.add(incident)
    await db.flush()

    def email_ok(**kw):
        return AlertDeliveryResult(delivered=True, channel="email", message_id="em1", attempts=1)

    result = await alerting.deliver_alert(
        db, incident, make_normalized(), make_scoring(),
        send_alert=_fail("slack"), send_pagerduty=_fail("pagerduty"), send_email=email_ok,
    )

    assert result.delivered is True
    assert incident.notification_channel == "email"
    assert incident.agent_alert_timestamp is not None


@pytest.mark.asyncio
async def test_all_channels_fail_records_no_timestamp(db):
    incident = make_incident()
    db.add(incident)
    await db.flush()

    result = await alerting.deliver_alert(
        db, incident, make_normalized(), make_scoring(),
        send_alert=_fail("slack"), send_pagerduty=_fail("pagerduty"), send_email=_fail("email"),
    )

    assert result.delivered is False
    assert incident.agent_alert_timestamp is None
    assert incident.notification_channel is None
    assert incident.notification_status == "failed"
    assert incident.status == "notification_failed"
    # The failed record keeps the Slack (primary) attempt count.
    assert incident.notification_attempts == 3


@pytest.mark.asyncio
async def test_slack_success_skips_fallbacks(db):
    incident = make_incident()
    db.add(incident)
    await db.flush()

    pd_calls = []

    def slack_ok(**kw):
        return AlertDeliveryResult(delivered=True, channel="slack", message_id="ts1", thread_ts="ts1", attempts=1)

    def pd_spy(**kw):
        pd_calls.append(1)
        return AlertDeliveryResult(delivered=True, channel="pagerduty")

    result = await alerting.deliver_alert(
        db, incident, make_normalized(), make_scoring(),
        send_alert=slack_ok, send_pagerduty=pd_spy, send_email=_fail("email"),
    )
    assert result.channel == "slack"
    assert incident.notification_channel == "slack"
    assert pd_calls == []          # fallbacks not invoked when Slack delivers


def test_channel_senders_not_configured_are_safe(monkeypatch):
    monkeypatch.setattr(settings, "PAGERDUTY_ROUTING_KEY", None, raising=False)
    monkeypatch.setattr(settings, "SMTP_HOST", None, raising=False)
    pd = channels.send_pagerduty_alert(incident_id="x", severity="critical", summary="s")
    em = channels.send_email_alert(incident_id="x", severity="critical", subject="s", body="b")
    assert pd.delivered is False and pd.error == "not_configured"
    assert em.delivered is False and em.error == "not_configured"
