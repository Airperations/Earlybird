"""
Earlybird — Database Configuration
Async SQLAlchemy with PostgreSQL.

Reads DATABASE_URL from the environment and normalizes it to the asyncpg driver.
No hardcoded value and no local fallback — a missing URL fails fast with a clear
message instead of a cryptic SQLAlchemy parse error deep in startup/migrations.
"""

import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base
from sqlalchemy.pool import NullPool

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable is not set")

# Railway/Heroku hand out `postgres://` or `postgresql://`. create_async_engine
# requires the asyncpg driver, so normalize to `postgresql+asyncpg://`. This keeps
# `DATABASE_URL=${{Postgres.DATABASE_URL}}` working as-is on Railway.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

# NullPool: the Celery workers run `asyncio.run(...)` per task, creating a fresh
# event loop each time. asyncpg connections are bound to their creating loop, so a
# pooled connection reused under a new loop raises "got Future attached to a
# different loop". NullPool opens/closes a connection per checkout — correct here.
# The FastAPI process runs a single persistent loop, so NullPool is safe there too.
engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    poolclass=NullPool,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

Base = declarative_base()


async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
