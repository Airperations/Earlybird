"""
Tests for fingerprint recurrence. `Incident.fingerprint` is globally unique, so a
recurrence REOPENS the same incident (resetting its race fields) rather than
inserting a duplicate. An active in-window incident keeps deduping normally.
"""

import pytest

from app.incidents import service
from tests.conftest import make_normalized


@pytest.mark.asyncio
async def test_active_incident_dedups_same_fingerprint(db):
    n = make_normalized(fingerprint="fp-recur")
    inc1 = await service.find_or_create_incident(db, n)
    await db.flush()
    inc2 = await service.find_or_create_incident(db, n)
    assert inc1.id == inc2.id
    assert inc2.event_count == 2


@pytest.mark.asyncio
async def test_resolved_incident_recurrence_reopens_same_incident(db):
    from datetime import datetime, timezone

    n = make_normalized(fingerprint="fp-recur-2")
    inc1 = await service.find_or_create_incident(db, n)
    await db.flush()
    original_id = inc1.id

    # The incident is alerted, then resolved.
    inc1.status = "resolved"
    inc1.agent_alert_timestamp = datetime.now(timezone.utc)
    await db.flush()

    # Same fingerprint fires again later — a recurrence. It reopens the same row
    # (unique fingerprint) and re-arms the race fields.
    inc2 = await service.find_or_create_incident(db, n)
    assert inc2.id == original_id
    assert inc2.status == "new"
    assert inc2.agent_alert_timestamp is None      # re-armed for a fresh race
    assert inc2.event_count == 2


@pytest.mark.asyncio
async def test_recurrence_writes_audit_entry(db):
    from sqlalchemy import select
    from app.models import AuditLog

    n = make_normalized(fingerprint="fp-recur-3")
    inc = await service.find_or_create_incident(db, n)
    await db.flush()
    inc.status = "resolved"
    await db.flush()

    await service.find_or_create_incident(db, n)
    rows = (await db.execute(
        select(AuditLog).where(AuditLog.event == "incident_recurred")
    )).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_distinct_users_tracked_across_recurrence(db):
    n1 = make_normalized(fingerprint="fp-users", user_id="a")
    n2 = make_normalized(fingerprint="fp-users", user_id="b")
    inc = await service.find_or_create_incident(db, n1)
    await db.flush()
    await service.find_or_create_incident(db, n2)
    assert inc.affected_users_count == 2
    assert set(inc.affected_user_ids) == {"a", "b"}
