"""
PII-safety tests. Raw user ids, emails, phones and secrets must never reach the
database (or by extension Slack/LLM/logs, which only ever read these fields).
"""

import json

import pytest
from sqlalchemy import select

from app import redaction
from app.redaction import hash_identifier, redact_payload
from app.incidents import service
from app.models import NormalizedEvent
from tests.conftest import make_normalized


def test_hash_identifier_is_deterministic_and_non_reversible():
    h1 = hash_identifier("real_user_123")
    h2 = hash_identifier("real_user_123")
    assert h1 == h2
    assert h1.startswith("u_")
    assert "real_user_123" not in h1
    assert hash_identifier("real_user_123") != hash_identifier("other_user")
    assert hash_identifier(None) is None
    assert hash_identifier("") is None


def test_redact_payload_hashes_flat_and_nested_ids():
    payload = {
        "user_id": "real_user_123",
        "user": {"id": "nested_user_456", "email": "victim@example.com"},
        "access_token": "sk-ant-supersecret-value",
        "phone": "+1 555 123 4567",
        "endpoint": "/withdraw/confirm",
    }
    cleaned = redact_payload(payload)
    blob = json.dumps(cleaned)

    assert "real_user_123" not in blob
    assert "nested_user_456" not in blob
    assert "victim@example.com" not in blob
    assert "supersecret" not in blob
    assert "555" not in blob                       # phone digits gone
    assert cleaned["user_id"].startswith("u_")
    assert cleaned["user"]["id"].startswith("u_")
    assert cleaned["user"]["email"] == "[EMAIL]"
    assert cleaned["access_token"] == "[REDACTED]"
    assert cleaned["endpoint"] == "/withdraw/confirm"   # non-PII preserved


@pytest.mark.asyncio
async def test_raw_user_id_is_not_stored_in_normalized_event(db):
    n = make_normalized(fingerprint="fp-pii", user_id="real_user_123")
    inc = await service.find_or_create_incident(db, n)
    await service.save_normalized_event(db, raw_event_id=None, normalized=n, incident=inc)
    await db.flush()

    rows = (await db.execute(select(NormalizedEvent))).scalars().all()
    assert len(rows) == 1
    assert rows[0].user_id != "real_user_123"
    assert rows[0].user_id == hash_identifier("real_user_123")

    # The incident tracks the hash, never the raw id.
    assert "real_user_123" not in (inc.affected_user_hashes or [])
    assert hash_identifier("real_user_123") in inc.affected_user_hashes
