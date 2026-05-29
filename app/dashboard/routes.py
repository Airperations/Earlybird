"""
Earlybird — Dashboard API Routes
Exposes the bounty metrics: win rate, incident table, lead times.
This is what the judges see.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, case
from datetime import datetime, timezone
from typing import List

from app.database import get_db
from app.models import Incident, IncidentFreshdeskMatch, FreshdeskTicket

router = APIRouter()


@router.get("/summary")
async def get_dashboard_summary(db: AsyncSession = Depends(get_db)):
    """
    Main bounty metrics summary.
    This is the scoreboard the judges evaluate.
    """
    # Total incidents
    total_result = await db.execute(select(func.count(Incident.id)))
    total_incidents = total_result.scalar()

    # Alerted incidents
    alerted_result = await db.execute(
        select(func.count(Incident.id)).where(Incident.agent_alert_timestamp.isnot(None))
    )
    total_alerted = alerted_result.scalar()

    # Match outcomes
    matches_result = await db.execute(select(IncidentFreshdeskMatch))
    matches = matches_result.scalars().all()

    agent_won = sum(1 for m in matches if m.outcome == "agent_won")
    agent_lost = sum(1 for m in matches if m.outcome == "agent_lost")
    ties = sum(1 for m in matches if m.outcome == "tie")
    total_matched = len(matches)

    win_rate = (agent_won / total_matched * 100) if total_matched > 0 else 0

    # Lead times (only for wins)
    win_deltas = [m.time_delta_seconds for m in matches if m.outcome == "agent_won" and m.time_delta_seconds]
    avg_lead_time = sum(win_deltas) / len(win_deltas) if win_deltas else 0
    median_lead_time = sorted(win_deltas)[len(win_deltas) // 2] if win_deltas else 0

    # Incidents without any ticket (prevented)
    prevented = total_alerted - total_matched

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "bounty_metric": {
            "win_rate_percent": round(win_rate, 1),
            "pass_bar_percent": 80,
            "passing": win_rate >= 80,
            "status": "✅ PASSING" if win_rate >= 80 else "❌ BELOW TARGET",
        },
        "incidents": {
            "total_detected": total_incidents,
            "total_alerted": total_alerted,
            "matched_to_freshdesk": total_matched,
            "prevented_no_ticket": prevented,
        },
        "race_results": {
            "agent_won": agent_won,
            "agent_lost": agent_lost,
            "ties": ties,
        },
        "lead_time": {
            "average_seconds": round(avg_lead_time),
            "median_seconds": round(median_lead_time),
            "average_human": _seconds_to_human(avg_lead_time),
            "median_human": _seconds_to_human(median_lead_time),
        },
    }


@router.get("/incidents")
async def get_incidents_table(db: AsyncSession = Depends(get_db)):
    """
    Full incident table with race results.
    The main evidence table for judges.
    """
    result = await db.execute(
        select(Incident)
        .order_by(Incident.first_seen_at.desc())
        .limit(100)
    )
    incidents = result.scalars().all()

    rows = []
    for inc in incidents:
        # Get match if exists
        match_result = await db.execute(
            select(IncidentFreshdeskMatch)
            .where(IncidentFreshdeskMatch.incident_id == inc.id)
            .limit(1)
        )
        match = match_result.scalar_one_or_none()

        rows.append({
            "incident_id": str(inc.id)[:8],
            "title": inc.title or inc.fingerprint,
            "severity": inc.severity,
            "score": inc.score,
            "status": inc.status,
            "affected_users": inc.affected_users_count,
            "event_count": inc.event_count,
            "countries": inc.countries,
            "first_seen_at": inc.first_seen_at.isoformat() if inc.first_seen_at else None,
            "agent_alert_timestamp": inc.agent_alert_timestamp.isoformat() if inc.agent_alert_timestamp else None,
            "freshdesk_ticket_id": match.freshdesk_ticket_id if match else None,
            "freshdesk_ticket_timestamp": match.freshdesk_ticket_timestamp.isoformat() if match else None,
            "time_delta_seconds": match.time_delta_seconds if match else None,
            "lead_time_human": _seconds_to_human(match.time_delta_seconds) if match else "No ticket yet",
            "outcome": match.outcome if match else ("alerted_no_ticket" if inc.agent_alert_timestamp else "observing"),
            "outcome_emoji": _outcome_emoji(match.outcome if match else None),
            "confidence": round(match.confidence, 2) if match else None,
        })

    return {"incidents": rows, "total": len(rows)}


@router.get("/win-rate")
async def get_win_rate(db: AsyncSession = Depends(get_db)):
    """Simple win rate endpoint for monitoring."""
    result = await db.execute(select(IncidentFreshdeskMatch))
    matches = result.scalars().all()

    total = len(matches)
    won = sum(1 for m in matches if m.outcome == "agent_won")
    rate = (won / total * 100) if total > 0 else 0

    return {
        "win_rate": round(rate, 1),
        "total_matched": total,
        "agent_won": won,
        "passing": rate >= 80,
    }


def _seconds_to_human(seconds) -> str:
    if not seconds:
        return "—"
    s = int(seconds)
    if s < 0:
        return f"-{_seconds_to_human(-s)}"
    if s < 60:
        return f"{s}s"
    elif s < 3600:
        return f"{s // 60}m {s % 60}s"
    else:
        return f"{s // 3600}h {(s % 3600) // 60}m"


def _outcome_emoji(outcome: str) -> str:
    return {
        "agent_won": "🏆 WON",
        "agent_lost": "❌ LOST",
        "tie": "🤝 TIE",
        None: "⏳ Pending",
    }.get(outcome, "❓")
