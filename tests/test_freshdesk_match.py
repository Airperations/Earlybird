"""Tests for Freshdesk matching confidence, win/loss logic, and immediate ingest."""

import uuid
from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy import select

from app.freshdesk import matcher
from app.freshdesk.ingest import ingest_ticket, extract_ticket
from app.models import Incident, IncidentFreshdeskMatch
from tests.conftest import make_incident


# ─── Pure win/loss classification ─────────────────────────────────────────────

def test_classify_outcome_win():
    assert matcher.classify_outcome(180) == "agent_won"      # ticket 3m after alert


def test_classify_outcome_loss():
    assert matcher.classify_outcome(-120) == "agent_lost"    # ticket 2m before alert


def test_classify_outcome_tie_grace():
    assert matcher.classify_outcome(-10) == "tie"            # within 30s grace
    assert matcher.classify_outcome(0) == "tie"


# ─── Hybrid confidence ────────────────────────────────────────────────────────

def _incident_for_confidence():
    inc = make_incident()
    inc.countries = ["MX"]
    inc.title = "Withdrawal failures in Mexico"
    inc.llm_summary = {"affected_area": "withdrawals"}
    inc.agent_alert_timestamp = datetime(2026, 5, 29, 12, 0, 0, tzinfo=timezone.utc)
    return inc


def test_strong_match_exceeds_threshold():
    inc = _incident_for_confidence()
    ticket = {
        "id": "1",
        "subject": "Cannot complete withdrawal",
        "description": "My withdrawal failed, money not received",
        "tags": ["MX"],
        "created_at": "2026-05-29T12:02:00Z",   # 2 min after alert
    }
    conf = matcher.calculate_match_confidence(inc, ticket)
    assert conf >= 0.5


def test_unrelated_ticket_low_confidence():
    inc = _incident_for_confidence()
    ticket = {
        "id": "2",
        "subject": "How do I change my profile picture?",
        "description": "Just a cosmetic question",
        "tags": ["DE"],
        "created_at": "2026-05-29T20:00:00Z",   # hours later, different country
    }
    conf = matcher.calculate_match_confidence(inc, ticket)
    assert conf < 0.5


def test_private_alias_still_works():
    # Backward-compat alias for older callers.
    assert matcher._calculate_match_confidence is matcher.calculate_match_confidence


# ─── Immediate webhook ingest ─────────────────────────────────────────────────

def test_extract_ticket_handles_nesting():
    assert extract_ticket({"freshdesk_webhook": {"id": 5}})["id"] == 5
    assert extract_ticket({"ticket": {"id": 6}})["id"] == 6
    assert extract_ticket({"id": 7})["id"] == 7


@pytest.mark.asyncio
async def test_webhook_immediate_save_and_match(db):
    # An incident the agent already alerted on, 3 minutes before the ticket.
    alert_ts = datetime(2026, 5, 29, 12, 0, 0, tzinfo=timezone.utc)
    inc = make_incident(status="alerted")
    inc.countries = ["MX"]
    inc.title = "Withdrawal failures in Mexico"
    inc.llm_summary = {"affected_area": "withdrawals"}
    inc.agent_alert_timestamp = alert_ts
    inc.notification_delivered_at = alert_ts
    inc.notification_status = "delivered"
    db.add(inc)
    await db.flush()

    payload = {
        "id": 9001,
        "subject": "Withdrawal not working",
        "description": "I tried to withdraw and it failed",
        "tags": ["MX"],
        "created_at": "2026-05-29T12:03:00Z",   # 3 min AFTER alert → agent won
    }

    result = await ingest_ticket(db, payload)
    assert result["saved"] is True

    # Ticket persisted immediately.
    from app.models import FreshdeskTicket
    saved = await db.get(FreshdeskTicket, "9001")
    assert saved is not None

    # Match recorded with the correct outcome.
    match = (await db.execute(select(IncidentFreshdeskMatch))).scalar_one_or_none()
    assert match is not None
    assert match.outcome == "agent_won"
    assert match.time_delta_seconds == 180
    assert inc.status == "matched_to_freshdesk"


@pytest.mark.asyncio
async def test_webhook_ingest_ignores_payload_without_id(db):
    result = await ingest_ticket(db, {"subject": "no id here"})
    assert result["saved"] is False
