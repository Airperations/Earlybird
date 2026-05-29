"""
Tests for the dashboard honesty metrics — a 10/10 system shows where it LOST,
not just where it won: p90 lead time, false positives, unmatched tickets, and
incidents detected early but lost to a delivery failure.
"""

import uuid
from datetime import datetime, timezone

import pytest

from app.dashboard.routes import get_dashboard_summary, _percentile
from app.models import Incident, FreshdeskTicket, IncidentFreshdeskMatch


def test_percentile_basics():
    assert _percentile([], 90) == 0
    assert _percentile([5], 90) == 5
    assert _percentile([10, 20, 30, 40, 50], 50) == 30
    assert _percentile([10, 20, 30, 40, 50], 90) == 50


def _incident(status, **kw):
    now = datetime.now(timezone.utc)
    base = dict(id=uuid.uuid4(), fingerprint=f"fp-{uuid.uuid4().hex[:6]}", status=status,
                first_seen_at=now, last_seen_at=now)
    base.update(kw)
    return Incident(**base)


@pytest.mark.asyncio
async def test_summary_surfaces_losses_and_gaps(db):
    alert_ts = datetime(2026, 5, 29, 12, 0, 0, tzinfo=timezone.utc)

    won = _incident("matched_to_freshdesk", agent_alert_timestamp=alert_ts,
                    notification_status="delivered", notification_delivered_at=alert_ts,
                    detected_at=alert_ts)
    lost = _incident("matched_to_freshdesk", agent_alert_timestamp=alert_ts,
                     notification_status="delivered", notification_delivered_at=alert_ts,
                     detected_at=alert_ts)
    false_pos = _incident("false_positive", detected_at=alert_ts)
    detected_failed = _incident("notification_failed", detected_at=alert_ts,
                                notification_status="failed")
    db.add_all([won, lost, false_pos, detected_failed])

    t1 = FreshdeskTicket(id="t1", subject="won ticket", created_at=alert_ts)
    t2 = FreshdeskTicket(id="t2", subject="lost ticket", created_at=alert_ts)
    t3 = FreshdeskTicket(id="t3", subject="unmatched", created_at=alert_ts)
    db.add_all([t1, t2, t3])
    await db.flush()

    db.add(IncidentFreshdeskMatch(
        id=uuid.uuid4(), incident_id=won.id, freshdesk_ticket_id="t1",
        agent_alert_timestamp=alert_ts, freshdesk_ticket_timestamp=alert_ts,
        time_delta_seconds=180, outcome="agent_won", confidence=0.9,
    ))
    db.add(IncidentFreshdeskMatch(
        id=uuid.uuid4(), incident_id=lost.id, freshdesk_ticket_id="t2",
        agent_alert_timestamp=alert_ts, freshdesk_ticket_timestamp=alert_ts,
        time_delta_seconds=-120, outcome="agent_lost", confidence=0.8,
    ))
    await db.flush()

    summary = await get_dashboard_summary(db)

    assert summary["official_win_rule"] == "agent_alert_timestamp < freshdesk_ticket_created_at"
    assert summary["race_results"]["agent_won"] == 1
    assert summary["race_results"]["agent_lost"] == 1
    assert summary["race_results"]["unmatched_freshdesk_tickets"] == 1
    assert summary["incidents"]["false_positives"] == 1
    assert summary["incidents"]["detected_but_delivery_failed"] == 1
    assert summary["lead_time"]["p90_seconds"] == 180
    assert summary["bounty_metric"]["win_rate_percent"] == 50.0
