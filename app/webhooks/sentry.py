"""
Earlybird — Sentry Webhook Receiver
Receives Sentry issue/error webhooks and immediately timestamps them.
"""

from fastapi import APIRouter, Request, HTTPException, BackgroundTasks
from datetime import datetime, timezone
import logging
import hashlib
import hmac

from app.config import settings
from app.workers.process_event import process_incoming_event

router = APIRouter()
logger = logging.getLogger(__name__)


def _verify_sentry_signature(payload: bytes, signature: str) -> bool:
    """Validate Sentry webhook signature if secret is configured."""
    if not settings.SENTRY_WEBHOOK_SECRET:
        return True  # Skip validation in dev mode
    expected = hmac.new(
        settings.SENTRY_WEBHOOK_SECRET.encode(),
        payload,
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature.replace("sha256=", ""))


@router.post("/")
async def receive_sentry_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Main Sentry webhook endpoint.
    ⚡ Critical path: timestamp → save → respond → process async.
    """
    # === STEP 1: Capture timestamp immediately ===
    received_at = datetime.now(timezone.utc)

    # === STEP 2: Read raw body ===
    body = await request.body()
    payload = await request.json()

    # === STEP 3: Optional signature validation ===
    sentry_sig = request.headers.get("sentry-hook-signature", "")
    if sentry_sig and not _verify_sentry_signature(body, sentry_sig):
        raise HTTPException(status_code=401, detail="Invalid Sentry signature")

    logger.info(f"[SENTRY] Webhook received at {received_at.isoformat()}")

    # === STEP 4: Enqueue for async processing ===
    process_incoming_event.delay(
        source="sentry",
        payload=payload,
        received_at=received_at.isoformat(),
    )

    # === STEP 5: Respond immediately — never block Sentry ===
    return {"status": "accepted", "received_at": received_at.isoformat()}
