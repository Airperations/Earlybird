"""
Earlybird — Freshdesk API Routes
Exposes endpoints for webhook ingestion and manual sync.
"""

from fastapi import APIRouter, Depends, BackgroundTasks, Request
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
import logging

from app.config import settings
from app.database import get_db
from app.webhooks.security import require_shared_secret, parse_json_or_400, enforce_replay_protection
from app.freshdesk.ingest import ingest_ticket
from app.workers.freshdesk_sync import sync_freshdesk_tickets

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/webhook")
async def freshdesk_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """
    Real-time Freshdesk webhook on ticket creation.
    Configure in Freshdesk: Admin > Automations > Webhooks > On Ticket Create,
    adding a custom header `x-webhook-token: <FRESHDESK_WEBHOOK_SECRET>`.

    The ticket is persisted AND matched immediately, inside this request — no
    waiting for the 60s poll — so the race outcome is recorded the instant a
    support ticket is created. A background full sync is still scheduled to pick
    up any fields/relations the webhook body omits.
    """
    received_at = datetime.now(timezone.utc)
    require_shared_secret(request, settings.FRESHDESK_WEBHOOK_SECRET, "freshdesk")
    await enforce_replay_protection(request, "freshdesk")
    payload = await parse_json_or_400(request)

    logger.info(f"[FRESHDESK WEBHOOK] Ticket received at {received_at.isoformat()}")

    # Immediate save + match (get_db commits on success).
    result = await ingest_ticket(db, payload)

    # Backfill via full sync for any data the webhook didn't carry.
    background_tasks.add_task(sync_freshdesk_tickets)

    return {"status": "accepted", "received_at": received_at.isoformat(), **result}


@router.post("/sync")
async def manual_sync():
    """Trigger a manual Freshdesk sync (useful for judges testing the system)."""
    sync_freshdesk_tickets.delay()
    return {"status": "sync_triggered"}


@router.get("/tickets")
async def list_tickets(db: AsyncSession = Depends(get_db)):
    """List all synced Freshdesk tickets."""
    from sqlalchemy import select
    from app.models import FreshdeskTicket
    result = await db.execute(
        select(FreshdeskTicket).order_by(FreshdeskTicket.created_at.desc()).limit(50)
    )
    tickets = result.scalars().all()
    return {
        "tickets": [
            {
                "id": t.id,
                "subject": t.subject,
                "requester_email": t.requester_email,
                "created_at": t.created_at.isoformat(),
                "tags": t.tags,
            }
            for t in tickets
        ]
    }
