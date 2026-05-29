"""
Tests for the recurrence redesign.

Official rule — each recurrence is its OWN benchmark race:
    same fingerprint + OPEN incident      → reuse the open incident
    same fingerprint + resolved/false_pos → create a NEW incident row

`incidents.fingerprint` is no longer globally unique; a partial unique index
allows only ONE open incident per fingerprint, so live duplicates can't form
while each closed recurrence gets a clean new row (clean id / first_seen /
agent_alert_timestamp / Freshdesk comparison).
"""

import pytest
from datetime import datetime, timezone
from sqlalchemy import select

from app.incidents import service
from app.models import AuditLog, Incident
from tests.conftest import make_normalized


@pytest.mark.asyncio
async def test_open_incident_same_fingerprint_is_reused(db):
    n = make_normalized(fingerprint="fp-open")
    inc1 = await service.find_or_create_incident(db, n)
    await db.flush()
    inc2 = await service.find_or_create_incident(db, n)
    assert inc1.id == inc2.id          # reused, not duplicated
    assert inc2.event_count == 2


@pytest.mark.asyncio
async def test_resolved_incident_recurrence_creates_new_row(db):
    n = make_normalized(fingerprint="fp-resolved")
    inc1 = await service.find_or_create_incident(db, n)
    await db.flush()
    inc1.status = "resolved"
    inc1.agent_alert_timestamp = datetime.now(timezone.utc)
    await db.flush()

    inc2 = await service.find_or_create_incident(db, n)
    await db.flush()

    assert inc2.id != inc1.id                     # brand-new race
    assert inc2.status == "new"
    assert inc2.agent_alert_timestamp is None     # clean benchmark field
    assert inc2.event_count == 1                  # its own counters
    # Two distinct rows now share the fingerprint.
    rows = (await db.execute(select(Incident).where(Incident.fingerprint == "fp-resolved"))).scalars().all()
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_false_positive_recurrence_creates_new_row(db):
    n = make_normalized(fingerprint="fp-fp")
    inc1 = await service.find_or_create_incident(db, n)
    await db.flush()
    inc1.status = "false_positive"
    await db.flush()

    inc2 = await service.find_or_create_incident(db, n)
    await db.flush()
    assert inc2.id != inc1.id
    assert inc2.status == "new"


@pytest.mark.asyncio
async def test_recurrence_does_not_raise_integrity_error(db):
    """A resolved incident + a recurrence must coexist without an IntegrityError."""
    n = make_normalized(fingerprint="fp-noerr")
    inc1 = await service.find_or_create_incident(db, n)
    await db.flush()
    inc1.status = "resolved"
    await db.flush()
    # Should not raise despite the partial unique index.
    inc2 = await service.find_or_create_incident(db, n)
    await db.flush()
    assert inc2.id != inc1.id


@pytest.mark.asyncio
async def test_two_open_incidents_same_fingerprint_are_rejected(db):
    """The partial unique index forbids two OPEN incidents per fingerprint."""
    from sqlalchemy.exc import IntegrityError
    import uuid

    now = datetime.now(timezone.utc)
    db.add(Incident(id=uuid.uuid4(), fingerprint="fp-dup", status="new",
                    first_seen_at=now, last_seen_at=now))
    await db.flush()
    db.add(Incident(id=uuid.uuid4(), fingerprint="fp-dup", status="alerted",
                    first_seen_at=now, last_seen_at=now))
    with pytest.raises(IntegrityError):
        await db.flush()


@pytest.mark.asyncio
async def test_recurrence_writes_audit_entry_per_recurrence(db):
    n = make_normalized(fingerprint="fp-audit")
    inc1 = await service.find_or_create_incident(db, n)
    await db.flush()
    inc1.status = "resolved"
    await db.flush()

    inc2 = await service.find_or_create_incident(db, n)
    await db.flush()

    rows = (await db.execute(
        select(AuditLog).where(AuditLog.event == "incident_recurred")
    )).scalars().all()
    assert len(rows) == 1
    # The recurrence audit entry belongs to the NEW incident and links the prior.
    assert rows[0].incident_id == inc2.id
    assert rows[0].details["previous_incident_id"] == str(inc1.id)


@pytest.mark.asyncio
async def test_distinct_users_tracked_within_an_open_incident(db):
    from app.redaction import hash_identifier
    n1 = make_normalized(fingerprint="fp-users", user_id="a")
    n2 = make_normalized(fingerprint="fp-users", user_id="b")
    inc = await service.find_or_create_incident(db, n1)
    await db.flush()
    await service.find_or_create_incident(db, n2)
    assert inc.affected_users_count == 2
    assert set(inc.affected_user_hashes) == {hash_identifier("a"), hash_identifier("b")}
