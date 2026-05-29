"""Tests for the incident state machine transitions."""

import pytest

from app.incidents import service
from tests.conftest import make_incident


def test_valid_transitions_allowed():
    assert service.can_transition("new", "detected")
    assert service.can_transition("detected", "alerted")
    assert service.can_transition("alerted", "enriched")
    assert service.can_transition("enriched", "matched_to_freshdesk")
    assert service.can_transition("detected", "notification_failed")
    assert service.can_transition("notification_failed", "alerted")  # retryable


def test_invalid_transitions_rejected():
    assert not service.can_transition("new", "enriched")          # skips delivery
    assert not service.can_transition("resolved", "alerted")
    assert not service.can_transition("ignored", "detected")      # terminal
    assert not service.can_transition("matched_to_freshdesk", "alerted")


def test_self_transition_is_noop_allowed():
    assert service.can_transition("alerted", "alerted")


@pytest.mark.asyncio
async def test_transition_incident_writes_audit_on_success(db):
    from sqlalchemy import select
    from app.models import AuditLog

    incident = make_incident(status="new")
    db.add(incident)
    await db.flush()

    ok = await service.transition_incident(db, incident, "detected", {"score": 90})
    assert ok is True
    assert incident.status == "detected"

    rows = (await db.execute(select(AuditLog).where(AuditLog.event == "state_transition"))).scalars().all()
    assert len(rows) == 1
    assert rows[0].details["from"] == "new"
    assert rows[0].details["to"] == "detected"


@pytest.mark.asyncio
async def test_transition_incident_rejects_illegal_without_changing_state(db):
    incident = make_incident(status="new")
    db.add(incident)
    await db.flush()

    ok = await service.transition_incident(db, incident, "enriched")
    assert ok is False
    assert incident.status == "new"   # unchanged
