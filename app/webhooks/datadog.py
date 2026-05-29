"""
Earlybird — Datadog Webhook Receiver
Receives Datadog monitor alerts and metric anomalies.
"""

from fastapi import APIRouter, Request, BackgroundTasks
from datetime import datetime, timezone
import logging

from app.workers.process_event import process_incoming_event

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/")
async def receive_datadog_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Datadog monitor alert webhook.
    Same pattern: timestamp first, process async.
    """
    received_at = datetime.now(timezone.utc)
    payload = await request.json()

    logger.info(f"[DATADOG] Webhook received at {received_at.isoformat()}")

    process_incoming_event.delay(
        source="datadog",
        payload=payload,
        received_at=received_at.isoformat(),
    )

    return {"status": "accepted", "received_at": received_at.isoformat()}
