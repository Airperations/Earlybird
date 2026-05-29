"""
Earlybird — Main FastAPI Application
Entry point for webhook ingestion and API endpoints.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
import redis.asyncio as aioredis
import uvicorn
import logging
from datetime import datetime, timezone

from app.config import settings
from app.database import engine, Base, get_db
from app.webhooks import sentry, datadog, product_events
from app.freshdesk import routes as freshdesk_routes
from app.dashboard import routes as dashboard_routes
from app.dashboard import visual as dashboard_visual
from app.incidents import service as incident_service

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Startup/shutdown via the modern lifespan API (replaces the deprecated
    @app.on_event hooks).

    Schema is managed by Alembic (`alembic upgrade head`), run as a deploy step
    before this process starts — see Procfile / docker-compose. We no longer call
    create_all here, because it cannot evolve an existing schema and masks drift.
    """
    logger.info("✅ Earlybird started successfully")
    yield
    await engine.dispose()
    logger.info("👋 Earlybird shut down cleanly")


# Create FastAPI app
app = FastAPI(
    title="Earlybird",
    description="Early Incident Detection & Freshdesk Benchmarking System",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    """Liveness: the process is up. Cheap, no external dependencies."""
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/ready")
async def ready():
    """Readiness: actually verify DB and Redis connectivity. 503 if either is down."""
    checks = {}
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["db"] = "ok"
    except Exception as e:
        checks["db"] = f"fail: {e.__class__.__name__}"

    redis_client = None
    try:
        redis_client = aioredis.from_url(settings.REDIS_URL)
        await redis_client.ping()
        checks["redis"] = "ok"
    except Exception as e:
        checks["redis"] = f"fail: {e.__class__.__name__}"
    finally:
        if redis_client is not None:
            await redis_client.aclose()

    if not all(v == "ok" for v in checks.values()):
        raise HTTPException(status_code=503, detail={"status": "not_ready", "checks": checks})
    return {"status": "ready", "version": "1.0.0", "checks": checks}


# Include routers
app.include_router(sentry.router, prefix="/webhooks/sentry", tags=["Webhooks"])
app.include_router(datadog.router, prefix="/webhooks/datadog", tags=["Webhooks"])
app.include_router(product_events.router, prefix="/events", tags=["Product Events"])
app.include_router(freshdesk_routes.router, prefix="/freshdesk", tags=["Freshdesk"])
app.include_router(dashboard_routes.router, prefix="/dashboard", tags=["Dashboard"])
# Visual dashboard (HTML + consolidated JSON). Separate router so /ui and /data
# can accept the key via header OR ?key= query param for browser access.
app.include_router(dashboard_visual.router, prefix="/dashboard", tags=["Dashboard"])


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
