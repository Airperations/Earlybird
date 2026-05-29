"""
Earlybird — Database Configuration
Async SQLAlchemy with PostgreSQL.
"""

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool
from app.config import settings

# NullPool is required because the Celery workers run `asyncio.run(...)` per task,
# spinning up a fresh event loop each time. asyncpg connections are bound to the
# loop that created them, so a pooled connection checked out under a new loop
# raises "got Future attached to a different loop". NullPool opens and closes a
# connection per checkout — slightly slower, but correct under this execution model.
# The FastAPI process runs a single persistent loop, so NullPool is safe there too.
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    poolclass=NullPool,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


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
