"""
Earlybird — Freshdesk Incident Matcher
Compares agent alert timestamps vs Freshdesk ticket timestamps.
This is the RACE RESULT calculator — the core bounty metric.
"""

import uuid
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models import Incident, FreshdeskTicket, IncidentFreshdeskMatch, AuditLog
from app.config import settings

logger = logging.getLogger(__name__)

# Keywords that indicate financial/platform issues in tickets
FINANCIAL_KEYWORDS = {
    "withdraw", "deposit", "transfer", "payment", "transaction",
    "p2p", "crypto", "balance", "send", "receive", "failed",
    "error", "not working", "issue", "problem", "can't", "unable",
    "retiro", "depósito", "transferencia", "pago", "saldo",
    "falla", "error", "no funciona", "problema",
}


def normalize_tags(raw) -> List[str]:
    """
    Normalize Freshdesk tags to a list of upper-cased strings.

    The real Freshdesk API v2 returns tags as plain strings (["MX", "withdrawal"]),
    but webhook automations / the demo script send dicts ([{"name": "MX"}]). Handle
    both so the matcher never crashes on `str.get(...)`.
    """
    out: List[str] = []
    for t in (raw or []):
        if isinstance(t, dict):
            name = t.get("name", "")
        else:
            name = t
        if name:
            out.append(str(name).upper())
    return out


def _calculate_match_confidence(incident: Incident, ticket: dict) -> float:
    """
    Calculate how likely this ticket relates to this incident.
    Uses multiple signals for robustness.
    Returns 0.0 to 1.0.
    """
    score = 0.0
    signals = 0

    ticket_text = (
        (ticket.get("subject") or "") + " " +
        (ticket.get("description_text") or ticket.get("description") or "")
    ).lower()

    # Signal 1: Endpoint keyword match
    if incident.fingerprint:
        endpoint_parts = [p for p in (incident.llm_summary or {}).get("affected_area", "").lower().split() if len(p) > 3]
        for part in endpoint_parts:
            if part in ticket_text:
                score += 0.3
                signals += 1
                break

    # Signal 2: Financial keyword overlap
    ticket_keywords = set(ticket_text.split()) & FINANCIAL_KEYWORDS
    if ticket_keywords:
        score += min(0.2, len(ticket_keywords) * 0.05)
        signals += 1

    # Signal 3: Country match (if ticket has tags)
    incident_countries = {str(c).upper() for c in (incident.countries or [])}
    ticket_tags = normalize_tags(ticket.get("tags"))
    if incident_countries & set(ticket_tags):
        score += 0.2
        signals += 1

    # Signal 4: Time proximity
    ticket_created = _parse_freshdesk_time(ticket.get("created_at"))
    if ticket_created and incident.agent_alert_timestamp:
        delta = abs((ticket_created - incident.agent_alert_timestamp).total_seconds())
        if delta <= 300:      # Within 5 min
            score += 0.3
        elif delta <= 900:    # Within 15 min
            score += 0.2
        elif delta <= 3600:   # Within 1 hour
            score += 0.1
        signals += 1

    return min(1.0, score)


async def match_incidents_to_freshdesk(
    db: AsyncSession,
    tickets: List[dict],
):
    """
    For each new Freshdesk ticket, find matching alerted incidents
    and record the race outcome.
    """
    # Get all alerted incidents without a match yet
    result = await db.execute(
        select(Incident)
        .where(Incident.status == "alerted")
        .where(Incident.agent_alert_timestamp.isnot(None))
    )
    incidents = result.scalars().all()

    for ticket in tickets:
        ticket_created = _parse_freshdesk_time(ticket.get("created_at"))
        if not ticket_created:
            continue

        ticket_id = str(ticket.get("id"))

        # Check if this ticket was already matched
        existing = await db.execute(
            select(IncidentFreshdeskMatch)
            .where(IncidentFreshdeskMatch.freshdesk_ticket_id == ticket_id)
        )
        if existing.scalar_one_or_none():
            continue

        best_incident = None
        best_confidence = 0.0

        for incident in incidents:
            # Only match within time window
            window_start = incident.agent_alert_timestamp - timedelta(minutes=10)
            window_end = incident.agent_alert_timestamp + timedelta(
                hours=settings.FRESHDESK_MATCH_WINDOW_HOURS
            )

            if not (window_start <= ticket_created <= window_end):
                continue

            confidence = _calculate_match_confidence(incident, ticket)
            if confidence > best_confidence:
                best_confidence = confidence
                best_incident = incident

        # Only record high-confidence matches
        if best_incident and best_confidence >= 0.5:
            await _record_match(db, best_incident, ticket, ticket_created, best_confidence)


async def _record_match(
    db: AsyncSession,
    incident: Incident,
    ticket: dict,
    ticket_created: datetime,
    confidence: float,
):
    """Record the race result between agent and Freshdesk."""
    agent_ts = incident.agent_alert_timestamp
    delta_seconds = int((ticket_created - agent_ts).total_seconds())

    # Positive delta = agent won (alerted before ticket)
    # Negative delta = agent lost (ticket arrived before alert)
    if delta_seconds > 0:
        outcome = "agent_won"
    elif delta_seconds < -30:   # 30s grace period for ties
        outcome = "agent_lost"
    else:
        outcome = "tie"

    match = IncidentFreshdeskMatch(
        id=uuid.uuid4(),
        incident_id=incident.id,
        freshdesk_ticket_id=str(ticket.get("id")),
        agent_alert_timestamp=agent_ts,
        freshdesk_ticket_timestamp=ticket_created,
        time_delta_seconds=delta_seconds,
        outcome=outcome,
        confidence=confidence,
        evidence={
            "ticket_subject": ticket.get("subject"),
            "ticket_tags": ticket.get("tags"),
            "incident_severity": incident.severity,
            "incident_score": incident.score,
            "incident_fingerprint": incident.fingerprint,
        },
    )
    db.add(match)

    # Update incident status
    incident.status = "matched_to_freshdesk"

    # Audit log
    audit = AuditLog(
        incident_id=incident.id,
        event=f"freshdesk_match_{outcome}",
        details={
            "ticket_id": str(ticket.get("id")),
            "delta_seconds": delta_seconds,
            "confidence": confidence,
            "outcome": outcome,
        },
    )
    db.add(audit)

    emoji = "🏆" if outcome == "agent_won" else "❌" if outcome == "agent_lost" else "🤝"
    logger.info(
        f"[MATCHER] {emoji} Incident {str(incident.id)[:8]} vs Ticket {ticket.get('id')}: "
        f"{outcome} (delta={delta_seconds}s, confidence={confidence:.2f})"
    )


def _parse_freshdesk_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
