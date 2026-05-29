"""
Earlybird — Datadog Webhook Receiver
Receives Datadog monitor alerts and metric anomalies.
"""

from fastapi import APIRouter, Request, BackgroundTasks
from datetime import datetime, timezone
import logging

from app.config import settings
from app.webhooks.security import require_shared_secret, parse_json_or_400, enforce_replay_protection
from app.workers.process_event import process_incoming_event

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/")
async def receive_datadog_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Datadog monitor alert webhook.
    Same pattern: timestamp first, process async.
    Configure Datadog to send header `x-webhook-token: <DATADOG_WEBHOOK_SECRET>`.
    """
    received_at = datetime.now(timezone.utc)
    require_shared_secret(request, settings.DATADOG_WEBHOOK_SECRET, "datadog")
    await enforce_replay_protection(request, "datadog")
    payload = await parse_json_or_400(request)

    logger.info(f"[DATADOG] Webhook received at {received_at.isoformat()}")

    process_incoming_event.delay(
        source="datadog",
        payload=payload,
        received_at=received_at.isoformat(),
    )

    return {"status": "accepted", "received_at": received_at.isoformat()}
