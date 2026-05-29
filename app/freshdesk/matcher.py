"""
Earlybird — Freshdesk Incident Matcher
Compares agent alert timestamps vs Freshdesk ticket timestamps.
This is the RACE RESULT calculator — the core bounty metric.
"""

import re
import uuid
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models import Incident, FreshdeskTicket, IncidentFreshdeskMatch, AuditLog
from app.redaction import redact_pii
from app.taxonomy import detect_keyword_overlap, base_action, ALL_KEYWORDS
from app.config import settings

logger = logging.getLogger(__name__)

# Backwards-compatible alias — the flat keyword set now lives in app.taxonomy.
FINANCIAL_KEYWORDS = ALL_KEYWORDS


@dataclass
class MatchExplanation:
    """
    A transparent, language-agnostic explanation of why a ticket matched an
    incident. `matched_by` uses a fixed generic vocabulary; `match_reasons`
    carries the human-readable specifics (including detected keyword language).
    """
    confidence: float
    matched_by: List[str] = field(default_factory=list)
    match_reasons: Dict = field(default_factory=dict)


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


def explain_match(incident: Incident, ticket: dict) -> MatchExplanation:
    """
    Hybrid, auditable match explanation — deliberately NOT LLM-only, so a model
    hallucination alone can neither fabricate nor destroy a match.

    Independent signal families, each contributing both confidence and a
    `matched_by` tag with structured reasons:
      • business_action — incident's normalized action appears in the ticket
      • country         — region tag overlap
      • provider        — incident provider named in the ticket
      • payment_method  — incident payment method named in the ticket
      • time_window     — proximity of ticket creation to the alert
      • keyword_match   — multilingual keyword-group overlap (language-agnostic)
      • (semantic)      — content-word overlap; folds into confidence only
    """
    score = 0.0
    matched_by: List[str] = []
    reasons: Dict = {}

    subject = ticket.get("subject") or ""
    description = ticket.get("description_text") or ticket.get("description") or ""
    ticket_text = f"{subject} {description}".lower()

    # Signal: business action (e.g. ticket about a "withdrawal" for a withdrawal incident).
    kw = detect_keyword_overlap(ticket_text, incident.business_action)
    if kw["action_match"] and incident.business_action:
        score += 0.3
        matched_by.append("business_action")
        reasons["business_action"] = incident.business_action
        reasons["normalized_business_action"] = base_action(incident.business_action)

    # Signal: country (metadata).
    incident_countries = {str(c).upper() for c in (incident.countries or [])}
    if incident.primary_country:
        incident_countries.add(str(incident.primary_country).upper())
    ticket_tags = set(normalize_tags(ticket.get("tags")))
    country_hit = incident_countries & ticket_tags
    if country_hit:
        score += 0.2
        matched_by.append("country")
        reasons["country"] = sorted(country_hit)[0]

    # Signal: provider named in the ticket text.
    if incident.provider and str(incident.provider).lower() in ticket_text:
        score += 0.15
        matched_by.append("provider")
        reasons["provider"] = incident.provider

    # Signal: payment method named in the ticket text.
    if incident.payment_method and str(incident.payment_method).lower() in ticket_text:
        score += 0.1
        matched_by.append("payment_method")
        reasons["payment_method"] = incident.payment_method

    # Signal: time proximity to the benchmark timestamp.
    ticket_created = _parse_freshdesk_time(ticket.get("created_at"))
    if ticket_created and incident.agent_alert_timestamp:
        signed = int((ticket_created - incident.agent_alert_timestamp).total_seconds())
        delta = abs(signed)
        if delta <= 300:
            score += 0.3
        elif delta <= 900:
            score += 0.2
        elif delta <= 3600:
            score += 0.1
        if delta <= 3600:
            matched_by.append("time_window")
            reasons["time_delta_seconds"] = signed

    # Signal: multilingual keyword-group overlap (generic label + detected language).
    if kw["overlap"]:
        score += min(0.2, len(kw["overlap"]) * 0.05)
        matched_by.append("keyword_match")
        reasons["keyword_overlap"] = kw["overlap"]
        reasons["keyword_group"] = kw["groups"]
        reasons["keyword_language"] = kw["language"]

    # Signal: semantic content-word overlap (confidence only; no matched_by tag).
    score += _semantic_overlap(incident, ticket_text)

    return MatchExplanation(
        confidence=min(1.0, score),
        matched_by=matched_by,
        match_reasons=reasons,
    )


def calculate_match_confidence(incident: Incident, ticket: dict) -> float:
    """Confidence-only wrapper around explain_match (back-compat for callers/tests)."""
    return explain_match(incident, ticket).confidence


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
        best_explanation: Optional[MatchExplanation] = None

        for incident in incidents:
            # Only match within the configurable time window.
            window_start = incident.agent_alert_timestamp - timedelta(minutes=10)
            window_end = incident.agent_alert_timestamp + timedelta(minutes=window_minutes)

            if not (window_start <= ticket_created <= window_end):
                continue

            explanation = explain_match(incident, ticket)
            if best_explanation is None or explanation.confidence > best_explanation.confidence:
                best_explanation = explanation
                best_incident = incident

        # Only record matches above the configurable confidence threshold.
        if best_incident and best_explanation and best_explanation.confidence >= confidence_threshold:
            await _record_match(db, best_incident, ticket, ticket_created, best_explanation)


async def _record_match(
    db: AsyncSession,
    incident: Incident,
    ticket: dict,
    ticket_created: datetime,
    explanation: MatchExplanation,
):
    """Record the race result between agent and Freshdesk."""
    agent_ts = incident.agent_alert_timestamp
    delta_seconds = int((ticket_created - agent_ts).total_seconds())

    # Positive delta = agent won (alerted before ticket); negative = agent lost.
    outcome = classify_outcome(delta_seconds)
    confidence = explanation.confidence

    match = IncidentFreshdeskMatch(
        id=uuid.uuid4(),
        incident_id=incident.id,
        freshdesk_ticket_id=str(ticket.get("id")),
        agent_alert_timestamp=agent_ts,
        freshdesk_ticket_timestamp=ticket_created,
        time_delta_seconds=delta_seconds,
        outcome=outcome,
        confidence=confidence,
        matched_by=explanation.matched_by,
        match_reasons=explanation.match_reasons,
        evidence={
            # Subject is redacted in case a user pasted PII into it.
            "ticket_subject": redact_pii(ticket.get("subject")),
            "ticket_tags": normalize_tags(ticket.get("tags")),
            "incident_severity": incident.severity,
            "incident_score": incident.score,
            "incident_fingerprint": incident.fingerprint,
            "incident_business_action": incident.business_action,
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
            "matched_by": explanation.matched_by,
            "match_reasons": explanation.match_reasons,
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
