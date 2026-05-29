"""
Earlybird — Main FastAPI Application
Entry point for webhook ingestion and API endpoints.
"""

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import uvicorn
import logging
from datetime import datetime, timezone

from app.config import settings
from app.database import engine, Base, get_db
from app.webhooks import sentry, datadog, product_events
from app.freshdesk import routes as freshdesk_routes
from app.dashboard import routes as dashboard_routes
from app.incidents import service as incident_service

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

# Create FastAPI app
app = FastAPI(
    title="Earlybird",
    description="Early Incident Detection & Freshdesk Benchmarking System",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    """Initialize database tables on startup."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("✅ Earlybird started successfully")


@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/ready")
async def ready():
    return {"status": "ready", "version": "1.0.0"}


# Include routers
app.include_router(sentry.router, prefix="/webhooks/sentry", tags=["Webhooks"])
app.include_router(datadog.router, prefix="/webhooks/datadog", tags=["Webhooks"])
app.include_router(product_events.router, prefix="/events", tags=["Product Events"])
app.include_router(freshdesk_routes.router, prefix="/freshdesk", tags=["Freshdesk"])
app.include_router(dashboard_routes.router, prefix="/dashboard", tags=["Dashboard"])


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
