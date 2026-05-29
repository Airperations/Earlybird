"""
Earlybird — Freshdesk Immediate Ingest

When Freshdesk fires its on-ticket-create webhook, we persist the ticket and run
the matcher RIGHT THEN — inside the request — instead of only flagging a later
poll. This minimises the window in which a support ticket exists but the race
hasn't been scored, which is exactly the comparison the judges audit.

`ingest_ticket` is transport-agnostic and session-injected so it can be unit
tested against in-memory SQLite without HTTP or Celery.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import FreshdeskTicket
from app.freshdesk.matcher import match_incidents_to_freshdesk, normalize_tags

logger = logging.getLogger(__name__)


def _parse_time(value) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return datetime.now(timezone.utc)


def extract_ticket(payload: dict) -> dict:
    """
    Normalise a Freshdesk webhook body into the flat ticket dict the matcher and
    model expect. Freshdesk automations can nest the ticket under
    `freshdesk_webhook` or `ticket`; handle both and the flat form.
    """
    if not isinstance(payload, dict):
        return {}
    ticket = payload.get("freshdesk_webhook") or payload.get("ticket") or payload
    return ticket


async def ingest_ticket(db: AsyncSession, payload: dict) -> dict:
    """
    Upsert a single ticket from a webhook payload and immediately run the matcher.
    Returns a small status dict. The caller owns the commit when it manages the
    session (e.g. FastAPI's get_db); we flush so the matcher sees the row.
    """
    ticket = extract_ticket(payload)
    ticket_id = ticket.get("id")
    if ticket_id is None:
        logger.warning("[FRESHDESK INGEST] Webhook payload has no ticket id — skipping")
        return {"saved": False, "reason": "no_ticket_id"}

    ticket_id = str(ticket_id)
    created_at = _parse_time(ticket.get("created_at"))

    existing = await db.get(FreshdeskTicket, ticket_id)
    if existing is None:
        db.add(FreshdeskTicket(
            id=ticket_id,
            subject=ticket.get("subject"),
            description=ticket.get("description_text") or ticket.get("description"),
            requester_email=(ticket.get("requester") or {}).get("email") if isinstance(ticket.get("requester"), dict) else ticket.get("requester_email"),
            tags=normalize_tags(ticket.get("tags")),
            created_at=created_at,
            raw_payload=ticket,
        ))
        await db.flush()
        logger.info(f"[FRESHDESK INGEST] Saved ticket {ticket_id} immediately from webhook")

    # Run the matcher right away so the race result is recorded instantly.
    # Ensure created_at is present for matching.
    match_ticket = dict(ticket)
    match_ticket.setdefault("created_at", created_at.isoformat())
    await match_incidents_to_freshdesk(db, [match_ticket])

    return {"saved": True, "ticket_id": ticket_id}
