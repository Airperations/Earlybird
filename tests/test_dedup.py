import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from app.database import Base
from app.normalizers.base import normalize_sentry
from app.incidents import service
import app.models  # noqa: F401  — register tables

PAYLOAD = {
    "project_slug": "payments-api",
    "event": {
        "tags": [["country_code", "MX"]],
        "request": {"url": "https://api/withdraw/confirm"},
        "exception": {"values": [{"type": "Boom", "value": "x"}]},
        "user": {"id": "u1"},
        "contexts": {"response": {"status_code": 502}},
    },
}


@pytest_asyncio.fixture
async def db():
    # In-memory async SQLite. JSONB/UUID degrade gracefully to JSON/CHAR here,
    # which is fine for exercising the dedup branch logic.
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    async with Session() as s:
        yield s
    await eng.dispose()


@pytest.mark.asyncio
async def test_same_fingerprint_dedups_into_one_incident(db):
    n = normalize_sentry(PAYLOAD)
    inc1 = await service.find_or_create_incident(db, n)
    await db.flush()
    inc2 = await service.find_or_create_incident(db, n)
    assert inc1.id == inc2.id          # deduped — same incident
    assert inc2.event_count == 2       # second event incremented the count


@pytest.mark.asyncio
async def test_distinct_users_are_tracked(db):
    # Regression guard for audit fix C4 — now tracked by SALTED HASH, never raw id.
    from app.redaction import hash_identifier
    n1 = normalize_sentry(PAYLOAD)
    p2 = {**PAYLOAD, "event": {**PAYLOAD["event"], "user": {"id": "u2"}}}
    n2 = normalize_sentry(p2)
    inc = await service.find_or_create_incident(db, n1)
    await db.flush()
    await service.find_or_create_incident(db, n2)
    assert inc.affected_users_count == 2
    assert set(inc.affected_user_hashes) == {hash_identifier("u1"), hash_identifier("u2")}
    # And crucially: the raw ids are NOT present.
    assert "u1" not in inc.affected_user_hashes
    assert "u2" not in inc.affected_user_hashes


@pytest.mark.asyncio
async def test_different_fingerprint_creates_two_incidents(db):
    a = normalize_sentry(PAYLOAD)
    b = normalize_sentry({**PAYLOAD, "event": {
        **PAYLOAD["event"], "request": {"url": "https://api/deposit/confirm"}}})
    inc_a = await service.find_or_create_incident(db, a)
    await db.flush()
    inc_b = await service.find_or_create_incident(db, b)
    assert inc_a.id != inc_b.id
