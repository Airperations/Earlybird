"""
Earlybird — Product Events Receiver
Accepts custom signals from Airdrive's own platform
(e.g. payment failures, auth spikes, KYC errors).
"""

from fastapi import APIRouter, Request, BackgroundTasks
from datetime import datetime, timezone
import logging

from app.workers.process_event import process_incoming_event

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/product")
async def receive_product_event(request: Request, background_tasks: BackgroundTasks):
    """
    Custom product event endpoint.
    Use this to send signals that Sentry/Datadog don't capture:
    - Business logic failures (payment declined ≠ HTTP 500)
    - Feature-level error rates
    - Proactive anomaly signals from the app itself
    """
    received_at = datetime.now(timezone.utc)
    payload = await request.json()

    logger.info(f"[PRODUCT] Event received at {received_at.isoformat()}")

    process_incoming_event.delay(
        source="product",
        payload=payload,
        received_at=received_at.isoformat(),
    )

    return {"status": "accepted", "received_at": received_at.isoformat()}
