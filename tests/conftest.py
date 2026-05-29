"""
Test-only shim: render Postgres JSONB as plain JSON when the bind is SQLite.

Production runs on PostgreSQL, so models legitimately use JSONB. The in-memory
SQLite used by the dedup tests can't compile JSONB, so we teach it to fall back
to JSON here. This does NOT affect production behavior.
"""
import os

# app.database now requires DATABASE_URL at import time (no local fallback).
# Provide a dummy so importing app modules under test never crashes; the dedup
# tests build their own in-memory SQLite engine and don't use this value.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

import uuid
from datetime import datetime, timezone, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession


@compiles(JSONB, "sqlite")
def _compile_jsonb_as_json_on_sqlite(element, compiler, **kw):
    return "JSON"


@pytest_asyncio.fixture
async def db():
    """In-memory async SQLite session with the full schema created."""
    from app.database import Base
    import app.models  # noqa: F401 — register tables

    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    async with Session() as s:
        yield s
    await eng.dispose()


def make_normalized(**overrides):
    """Build a NormalizedEventSchema with sensible defaults for tests."""
    from app.normalizers.base import NormalizedEventSchema
    defaults = dict(
        source="product", service="payments-api", environment="production",
        endpoint="/withdraw/confirm", url="https://api/withdraw/confirm",
        http_status=502, exception_type="GatewayTimeout", message="boom",
        user_id="u1", country="MX", platform="ios", release="v1",
        fingerprint="fp-test", raw_payload={},
    )
    defaults.update(overrides)
    return NormalizedEventSchema(**defaults)


def make_scoring(total_score=125, severity="critical", is_critical_path=True, owner="payments"):
    from app.incidents.scoring import ScoringResult
    return ScoringResult(
        total_score=total_score, severity=severity, breakdown={"critical_path": 50},
        suggested_owner=owner, is_critical_path=is_critical_path,
    )


def make_incident(status="observing", **overrides):
    """Build an Incident ORM object (not yet added to a session)."""
    from app.models import Incident
    now = datetime.now(timezone.utc)
    defaults = dict(
        id=uuid.uuid4(), fingerprint="fp-test", status=status,
        first_seen_at=now, last_seen_at=now, affected_users_count=3,
        affected_user_ids=["u1", "u2", "u3"], event_count=5, countries=["MX"],
        notification_status="pending", notification_attempts=0,
    )
    defaults.update(overrides)
    return Incident(**defaults)
