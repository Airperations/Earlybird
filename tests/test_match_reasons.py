"""
Tests for structured, language-agnostic Freshdesk match explanations.

The matcher returns confidence + matched_by + match_reasons. `matched_by` uses a
generic vocabulary (never "spanish_keyword"); the detected language lives in
match_reasons.keyword_language.
"""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.freshdesk import matcher
from app.freshdesk.ingest import ingest_ticket
from app.models import IncidentFreshdeskMatch
from tests.conftest import make_incident


def _incident():
    inc = make_incident(status="alerted")
    inc.countries = ["MX"]
    inc.primary_country = "MX"
    inc.business_action = "withdrawal_failed"
    inc.provider = "stripe"
    inc.agent_alert_timestamp = datetime(2026, 5, 29, 12, 0, 0, tzinfo=timezone.utc)
    return inc


def test_explain_match_spanish():
    inc = _incident()
    ticket = {
        "id": "1",
        "subject": "No me deja hacer un retiro",
        "description": "Intenté retirar con stripe y mi saldo está mal",
        "tags": ["MX"],
        "created_at": "2026-05-29T12:04:00Z",   # +240s
    }
    ex = matcher.explain_match(inc, ticket)
    assert "business_action" in ex.matched_by
    assert "country" in ex.matched_by
    assert "provider" in ex.matched_by
    assert "time_window" in ex.matched_by
    assert "keyword_match" in ex.matched_by
    # Generic label only — no language-specific tag leaks into matched_by.
    assert "spanish_keyword" not in ex.matched_by
    assert ex.match_reasons["business_action"] == "withdrawal_failed"
    assert ex.match_reasons["normalized_business_action"] == "withdrawal"
    assert ex.match_reasons["country"] == "MX"
    assert ex.match_reasons["provider"] == "stripe"
    assert ex.match_reasons["keyword_language"] == "es"
    assert "retiro" in ex.match_reasons["keyword_overlap"]
    assert ex.match_reasons["time_delta_seconds"] == 240
    assert ex.confidence >= 0.5


def test_explain_match_english():
    inc = _incident()
    inc.primary_country = "US"
    inc.countries = ["US"]
    ticket = {
        "id": "2",
        "subject": "Withdrawal failed",
        "description": "My withdrawal failed via stripe",
        "tags": ["US"],
        "created_at": "2026-05-29T12:03:00Z",   # +180s
    }
    ex = matcher.explain_match(inc, ticket)
    assert ex.match_reasons["keyword_language"] == "en"
    assert ex.match_reasons["country"] == "US"
    assert ex.match_reasons["time_delta_seconds"] == 180
    assert "withdrawal" in ex.match_reasons["keyword_overlap"]


def test_explain_match_unrelated_is_low_and_empty():
    inc = _incident()
    ticket = {
        "id": "3",
        "subject": "How do I change my profile picture?",
        "description": "Just cosmetic",
        "tags": ["DE"],
        "created_at": "2026-05-29T20:00:00Z",
    }
    ex = matcher.explain_match(inc, ticket)
    assert ex.confidence < 0.5
    assert "business_action" not in ex.matched_by
    assert "country" not in ex.matched_by


def test_calculate_match_confidence_wrapper_still_returns_float():
    inc = _incident()
    ticket = {"id": "4", "subject": "withdrawal failed", "tags": ["MX"],
              "created_at": "2026-05-29T12:02:00Z"}
    conf = matcher.calculate_match_confidence(inc, ticket)
    assert isinstance(conf, float)
    assert matcher._calculate_match_confidence is matcher.calculate_match_confidence


@pytest.mark.asyncio
async def test_match_reasons_persisted_on_match(db):
    inc = _incident()
    inc.notification_delivered_at = inc.agent_alert_timestamp
    inc.notification_status = "delivered"
    db.add(inc)
    await db.flush()

    payload = {
        "id": 7777,
        "subject": "No me deja retirar",
        "description": "error con stripe",
        "tags": ["MX"],
        "created_at": "2026-05-29T12:03:00Z",
    }
    await ingest_ticket(db, payload)

    match = (await db.execute(select(IncidentFreshdeskMatch))).scalar_one()
    assert match.outcome == "agent_won"
    assert "keyword_match" in match.matched_by
    assert match.match_reasons["keyword_language"] in ("es", "mixed")
    assert match.match_reasons["business_action"] == "withdrawal_failed"
