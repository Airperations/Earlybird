"""
Earlybird — Freshdesk Sync Worker
Periodically polls Freshdesk for new tickets and runs the matcher.
Scheduled via Celery Beat every 60 seconds.
"""

import asyncio
import uuid
import logging
from datetime import datetime, timezone, timedelta

from app.celery_app import celery_app
from app.database import AsyncSessionLocal
from app.models import FreshdeskTicket
from app.freshdesk.client import freshdesk_client
from app.freshdesk.matcher import match_incidents_to_freshdesk

logger = logging.getLogger(__name__)


@celery_app.task(name="app.workers.freshdesk_sync.sync_freshdesk_tickets")
def sync_freshdesk_tickets():
    """Periodic task: fetch new Freshdesk tickets and run matcher."""
    asyncio.run(_sync())


async def _sync():
    since = datetime.now(timezone.utc) - timedelta(hours=2)

    logger.info(f"[FRESHDESK SYNC] Fetching tickets since {since.isoformat()}")

    tickets = freshdesk_client.get_tickets(since=since, per_page=100)

    if not tickets:
        logger.info("[FRESHDESK SYNC] No new tickets")
        return

    logger.info(f"[FRESHDESK SYNC] Got {len(tickets)} tickets")

    async with AsyncSessionLocal() as db:
        # Save new tickets to DB
        for ticket in tickets:
            ticket_id = str(ticket.get("id"))
            existing = await db.get(FreshdeskTicket, ticket_id)
            if not existing:
                created_at_raw = ticket.get("created_at")
                try:
                    created_at = datetime.fromisoformat(
                        str(created_at_raw).replace("Z", "+00:00")
                    )
                except Exception:
                    created_at = datetime.now(timezone.utc)

                fd_ticket = FreshdeskTicket(
                    id=ticket_id,
                    subject=ticket.get("subject"),
                    description=ticket.get("description_text") or ticket.get("description"),
                    requester_email=ticket.get("requester", {}).get("email"),
                    tags=ticket.get("tags", []),
                    created_at=created_at,
                    raw_payload=ticket,
                )
                db.add(fd_ticket)

        await db.flush()

        # Run matcher against alerted incidents
        await match_incidents_to_freshdesk(db, tickets)

        await db.commit()

    logger.info("[FRESHDESK SYNC] ✅ Sync complete")
