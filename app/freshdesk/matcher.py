"""
Earlybird — Freshdesk Incident Matcher
Compares agent alert timestamps vs Freshdesk ticket timestamps.
This is the RACE RESULT calculator — the core bounty metric.
"""

import re
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


_TOKEN = re.compile(r"[a-záéíóúñü]+", re.IGNORECASE)
_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "have", "your", "you",
    "are", "was", "but", "not", "can", "all", "any", "una", "los", "las", "del",
    "que", "con", "por", "para", "una", "como", "esta", "este", "mi", "me",
}


def _incident_text(incident: Incident) -> str:
    """All free text we know about an incident, lower-cased, for semantic overlap."""
    summary = incident.llm_summary or {}
    parts = [
        incident.title or "",
        incident.fingerprint or "",
        summary.get("affected_area", ""),
        summary.get("title", ""),
        summary.get("summary", ""),
    ]
    return " ".join(p for p in parts if p).lower()


def _semantic_overlap(incident: Incident, ticket_text: str) -> float:
    """
    Lightweight token-overlap ('semantic') signal: Jaccard-style overlap of
    content words between the incident's known text and the ticket text. Cheap,
    dependency-free, and language-agnostic — a complement to, not a replacement
    for, the metadata/time/keyword signals.
    """
    inc_tokens = {t for t in _TOKEN.findall(_incident_text(incident)) if len(t) > 3 and t not in _STOPWORDS}
    tkt_tokens = {t for t in _TOKEN.findall(ticket_text) if len(t) > 3 and t not in _STOPWORDS}
    if not inc_tokens or not tkt_tokens:
        return 0.0
    overlap = inc_tokens & tkt_tokens
    if not overlap:
        return 0.0
    jaccard = len(overlap) / len(inc_tokens | tkt_tokens)
    return min(0.2, jaccard * 0.6)


def calculate_match_confidence(incident: Incident, ticket: dict) -> float:
    """
    Hybrid confidence that a ticket relates to an incident, in [0.0, 1.0].

    Four independent signal families — deliberately NOT LLM-only, so a model
    hallucination can't fabricate a match:
      • metadata  — country/region tag overlap
      • time      — proximity of ticket creation to the alert
      • keyword   — financial/platform vocabulary overlap (EN + ES)
      • semantic  — content-word overlap with the incident's known text
    """
    score = 0.0

    ticket_text = (
        (ticket.get("subject") or "") + " " +
        (ticket.get("description_text") or ticket.get("description") or "")
    ).lower()

    # Signal 1: Endpoint / affected-area keyword match
    endpoint_parts = [p for p in (incident.llm_summary or {}).get("affected_area", "").lower().split() if len(p) > 3]
    for part in endpoint_parts:
        if part in ticket_text:
            score += 0.3
            break

    # Signal 2: Financial keyword overlap (keyword signal)
    ticket_keywords = set(ticket_text.split()) & FINANCIAL_KEYWORDS
    if ticket_keywords:
        score += min(0.2, len(ticket_keywords) * 0.05)

    # Signal 3: Country match (metadata signal)
    incident_countries = {str(c).upper() for c in (incident.countries or [])}
    ticket_tags = normalize_tags(ticket.get("tags"))
    if incident_countries & set(ticket_tags):
        score += 0.2

    # Signal 4: Time proximity (time signal)
    ticket_created = _parse_freshdesk_time(ticket.get("created_at"))
    if ticket_created and incident.agent_alert_timestamp:
        delta = abs((ticket_created - incident.agent_alert_timestamp).total_seconds())
        if delta <= 300:      # Within 5 min
            score += 0.3
        elif delta <= 900:    # Within 15 min
            score += 0.2
        elif delta <= 3600:   # Within 1 hour
            score += 0.1

    # Signal 5: Semantic content-word overlap
    score += _semantic_overlap(incident, ticket_text)

    return min(1.0, score)


# Backward-compatible alias (older callers / tests used the private name).
_calculate_match_confidence = calculate_match_confidence


def classify_outcome(delta_seconds: int, tie_grace_seconds: int = 30) -> str:
    """
    The benchmark win/loss rule, isolated and pure for testability.

    delta = ticket_created - agent_alert_timestamp (seconds).
      > 0                    → agent alerted first  → agent_won
      <= -tie_grace_seconds  → ticket arrived first → agent_lost
      otherwise              → effectively simultaneous → tie
    """
    if delta_seconds > 0:
        return "agent_won"
    if delta_seconds < -tie_grace_seconds:
        return "agent_lost"
    return "tie"


async def match_incidents_to_freshdesk(
    db: AsyncSession,
    tickets: List[dict],
):
    """
    For each new Freshdesk ticket, find matching alerted incidents
    and record the race outcome.
    """
    # Candidate incidents: a delivered alert (benchmark timestamp set) that is not
    # already matched or closed. This includes both 'alerted' and 'enriched'.
    result = await db.execute(
        select(Incident)
        .where(Incident.agent_alert_timestamp.isnot(None))
        .where(Incident.status.not_in(["matched_to_freshdesk", "resolved", "ignored", "false_positive"]))
    )
    incidents = result.scalars().all()

    # A ticket can arrive slightly before the alert (the agent can still win on a
    # later event), so allow a small look-back plus the forward match window.
    window_minutes = settings.FRESHDESK_MATCH_TIME_WINDOW_MINUTES
    confidence_threshold = settings.FRESHDESK_MATCH_CONFIDENCE_THRESHOLD

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
            # Only match within the configurable time window.
            window_start = incident.agent_alert_timestamp - timedelta(minutes=10)
            window_end = incident.agent_alert_timestamp + timedelta(minutes=window_minutes)

            if not (window_start <= ticket_created <= window_end):
                continue

            confidence = calculate_match_confidence(incident, ticket)
            if confidence > best_confidence:
                best_confidence = confidence
                best_incident = incident

        # Only record matches above the configurable confidence threshold.
        if best_incident and best_confidence >= confidence_threshold:
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

    # Positive delta = agent won (alerted before ticket); negative = agent lost.
    outcome = classify_outcome(delta_seconds)

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
