"""
Earlybird — Sentry Webhook Receiver
Receives Sentry issue/error webhooks and immediately timestamps them.
"""

from fastapi import APIRouter, Request, HTTPException, BackgroundTasks
from datetime import datetime, timezone
import json
import logging

from app.config import settings
from app.webhooks.security import verify_hmac_signature, parse_json_or_400
from app.workers.process_event import process_incoming_event

router = APIRouter()
logger = logging.getLogger(__name__)


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

    # === STEP 3: Signature validation — ENFORCED when a secret is set ===
    # Gating on the header's presence would let an attacker bypass it by simply
    # omitting the header. When a secret is configured, a valid signature is required.
    if settings.SENTRY_WEBHOOK_SECRET:
        sig = request.headers.get("sentry-hook-signature", "")
        if not verify_hmac_signature(body, sig, settings.SENTRY_WEBHOOK_SECRET):
            raise HTTPException(status_code=401, detail="Invalid or missing Sentry signature")

    try:
        payload = json.loads(body) if body else {}
    except Exception:
        raise HTTPException(status_code=400, detail="Request body must be valid JSON")

    logger.info(f"[SENTRY] Webhook received at {received_at.isoformat()}")

    # === STEP 4: Enqueue for async processing ===
    process_incoming_event.delay(
        source="sentry",
        payload=payload,
        received_at=received_at.isoformat(),
    )

    # === STEP 5: Respond immediately — never block Sentry ===
    return {"status": "accepted", "received_at": received_at.isoformat()}
