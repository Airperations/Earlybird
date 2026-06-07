"""
Earlybird — Visual Production Dashboard.

A lightweight, read-only browser dashboard for the last 30 days of Earlybird
product/agent metrics. It renders server-side HTML with inline CSS (no React/Vue/
Next, no external CSS/JS) and a consolidated JSON sibling endpoint.

Routes (mounted under /dashboard):
    GET  /dashboard/login  → browser login form
    POST /dashboard/login  → exchange the key for a signed session cookie
    GET  /dashboard/logout → clear the session cookie
    GET  /dashboard/ui     → the visual HTML page
    GET  /dashboard/data   → the same data as a consolidated JSON payload

These routes intentionally live on their own router (NOT the header-only
`require_dashboard_key` router). The recommended way in is the login form: the
key is POSTed once and exchanged for an HttpOnly session cookie, so it never
lands in the URL or browser history. For debugging, the `x-dashboard-key`
header and a `?key=` query parameter are still accepted. The existing JSON
endpoints (/dashboard/summary, /dashboard/metrics, /dashboard/audit) are
untouched.

Privacy: this view never exposes raw payloads, raw user identifiers, hashed user
references, requester emails, API keys, webhook secrets, or the dashboard key.
"""

import hashlib
import hmac
import html
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, Form, Header, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import (
    Incident,
    IncidentFreshdeskMatch,
    FreshdeskTicket,
    MetricBucket,
    RawEvent,
)
from app.dashboard.routes import _percentile, _seconds_to_human, _outcome_emoji

logger = logging.getLogger(__name__)

router = APIRouter()

WINDOW_DAYS = 30

# Static production links (no secrets — these are public ingestion URLs / probes).
PRODUCTION_LINKS = {
    "datadog_webhook": "https://earlybird-production-e5b5.up.railway.app/webhooks/datadog/",
    "sentry_webhook": "https://earlybird-production-e5b5.up.railway.app/webhooks/sentry/",
    "freshdesk_webhook": "https://earlybird-production-e5b5.up.railway.app/freshdesk/webhook",
    "health": "/health",
    "ready": "/ready",
}

OFFICIAL_WIN_RULE = "agent_alert_timestamp < freshdesk_ticket_created_at"


# ─── Auth (browser login cookie · header · query param) ───────────────────────

# Name of the signed session cookie set by the browser login form. The cookie
# never stores the key itself — only a key-derived token (see _session_token).
SESSION_COOKIE_NAME = "eb_dashboard_session"

# Fixed, non-secret salt mixed into the session token. Bumping the version
# suffix invalidates every previously issued cookie.
_SESSION_SALT = b"earlybird-dashboard-session-v1"


def _session_token(expected_key: str) -> str:
    """
    Derive the opaque session-cookie value from the configured dashboard key.

    It's an HMAC of a fixed salt keyed by DASHBOARD_API_KEY, so the cookie
    proves the holder authenticated without ever embedding the key. Rotating the
    key (or the salt version) invalidates all outstanding cookies automatically.
    """
    return hmac.new(expected_key.encode("utf-8"), _SESSION_SALT, hashlib.sha256).hexdigest()


def _key_is_valid(provided: Optional[str]) -> bool:
    """
    Accept the dashboard key from either the `x-dashboard-key` header or a
    `?key=` query param. When DASHBOARD_API_KEY is unset, access is open (dev/
    demo) — mirroring the existing JSON endpoints' behavior. The key is never
    logged here.
    """
    expected = settings.DASHBOARD_API_KEY
    if not expected:
        logger.warning("[DASHBOARD] DASHBOARD_API_KEY not set — visual dashboard is UNAUTHENTICATED")
        return True
    return bool(provided and hmac.compare_digest(provided, expected))


def _is_authorized(provided: Optional[str], session_cookie: Optional[str]) -> bool:
    """
    A request is authorized if it carries the raw key (header or `?key=`, kept
    for debugging) OR a valid browser-login session cookie. When no key is
    configured, access is open (dev/demo) — _key_is_valid handles that branch.
    """
    expected = settings.DASHBOARD_API_KEY
    if not expected:
        return _key_is_valid(provided)
    if provided and hmac.compare_digest(provided, expected):
        return True
    if session_cookie and hmac.compare_digest(session_cookie, _session_token(expected)):
        return True
    return False


def _set_session_cookie(response, request: Request, expected_key: str) -> None:
    """Issue the signed session cookie. Marked Secure when served over HTTPS."""
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=_session_token(expected_key),
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        max_age=60 * 60 * 12,  # 12h — judges/operators re-login next day.
        path="/dashboard",
    )


# ─── Metric-bucket anomaly cells (reuses the live anomaly logic) ──────────────

async def _metric_cells(db: AsyncSession, now: datetime):
    """
    Compute current-vs-baseline metric cells exactly like /dashboard/metrics,
    returning (cells, active_anomaly_count). Anomaly windows are minute-scale by
    design (a real-time signal), independent of the 30-day reporting window.
    """
    from app.incidents.metrics import floor_minute, analyze_series, _fold, as_aware
    from app.taxonomy import CRITICAL_ACTIONS

    current_minutes = settings.ANOMALY_CURRENT_WINDOW_MINUTES
    baseline_minutes = settings.ANOMALY_BASELINE_WINDOW_MINUTES
    current_start = floor_minute(now) - timedelta(minutes=current_minutes - 1)
    baseline_start = current_start - timedelta(minutes=baseline_minutes)

    rows = (await db.execute(
        select(MetricBucket)
        .where(MetricBucket.bucket_start >= baseline_start)
        .order_by(MetricBucket.bucket_start.asc())
    )).scalars().all()

    cells: dict = {}
    for b in rows:
        cells.setdefault((b.business_action, b.dimension, b.dimension_value), []).append(b)

    out = []
    for (action, dimension, value), buckets in cells.items():
        current = [b for b in buckets if as_aware(b.bucket_start) >= current_start]
        baseline = [b for b in buckets if as_aware(b.bucket_start) < current_start]
        if not current:
            continue
        cur, base = _fold(current), _fold(baseline)
        result = analyze_series(current, baseline, action=action, dimension=dimension,
                                value=value, critical=action in CRITICAL_ACTIONS)
        out.append({
            "business_action": action,
            "dimension": dimension,
            "dimension_value": value,
            "current_total": cur.total,
            "current_success_rate": round(cur.success_rate, 3) if cur.success_rate is not None else None,
            "baseline_total": base.total,
            "baseline_success_rate": round(base.success_rate, 3) if base.success_rate is not None else None,
            "anomaly_kind": result.kind if result.is_anomaly else None,
            "severity_boost": result.severity_boost if result.is_anomaly else None,
        })

    out.sort(key=lambda c: (c["anomaly_kind"] is not None, c["current_total"]), reverse=True)
    active = sum(1 for c in out if c["anomaly_kind"])
    return out, active


# ─── Consolidated data builder (shared by /data and /ui) ──────────────────────

async def build_dashboard_data(db: AsyncSession, now: Optional[datetime] = None) -> dict:
    """
    Build the full read-only dashboard payload for the last 30 days. Scoped to
    incidents whose first_seen_at falls inside the window; matches are attributed
    to those incidents. Returns plain JSON-serializable data — no raw payloads,
    user identifiers, emails, or secrets.
    """
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=WINDOW_DAYS)
    stale_cutoff = now - timedelta(hours=24)

    # Incidents in window (most recent first).
    incidents = (await db.execute(
        select(Incident)
        .where(Incident.first_seen_at >= since)
        .order_by(Incident.first_seen_at.desc())
    )).scalars().all()
    inc_ids = {inc.id for inc in incidents}

    # Matches attributed to in-window incidents.
    all_matches = (await db.execute(select(IncidentFreshdeskMatch))).scalars().all()
    matches = [m for m in all_matches if m.incident_id in inc_ids]
    match_by_incident = {m.incident_id: m for m in matches}

    # ── Race aggregates ──────────────────────────────────────────────────────
    wins = sum(1 for m in matches if m.outcome == "agent_won")
    losses = sum(1 for m in matches if m.outcome == "agent_lost")
    ties = sum(1 for m in matches if m.outcome == "tie")
    total_matched = len(matches)
    win_rate = round((wins / total_matched * 100), 1) if total_matched else 0.0

    win_deltas = sorted(
        m.time_delta_seconds for m in matches
        if m.outcome == "agent_won" and m.time_delta_seconds
    )
    median_lead = _percentile(win_deltas, 50)
    p90_lead = _percentile(win_deltas, 90)

    # ── Incident-level counters ──────────────────────────────────────────────
    total_incidents = len(incidents)
    critical_incidents = sum(1 for i in incidents if i.severity == "critical")
    alerts_delivered = sum(1 for i in incidents if i.notification_status == "delivered")
    notification_failures = sum(1 for i in incidents if i.notification_status == "failed")
    false_positives = sum(1 for i in incidents if i.status == "false_positive")
    pending_unmatched = sum(1 for i in incidents if i.id not in match_by_incident)
    detected_failed = sum(
        1 for i in incidents
        if i.detected_at is not None and i.notification_status == "failed"
    )

    # Freshdesk tickets seen in window + how many we ever matched.
    tickets_seen = (await db.execute(
        select(func.count(FreshdeskTicket.id)).where(FreshdeskTicket.created_at >= since)
    )).scalar() or 0
    matched_ticket_ids = {m.freshdesk_ticket_id for m in matches}
    unmatched_tickets = max(0, tickets_seen - len(matched_ticket_ids))

    # ── Metric-bucket anomalies ──────────────────────────────────────────────
    metric_cells, active_anomalies = await _metric_cells(db, now)

    # ── Benchmark card: the latest race outcome ──────────────────────────────
    benchmark = {
        "official_rule": OFFICIAL_WIN_RULE,
        "latest_outcome": "PENDING",
        "latest_outcome_label": "⏳ PENDING",
        "latest_lead_time_seconds": None,
        "latest_lead_time_human": "—",
        "latest_freshdesk_ticket_id": None,
        "latest_incident_id": None,
    }
    if matches:
        latest = max(matches, key=lambda m: m.freshdesk_ticket_timestamp or now)
        outcome_word = {"agent_won": "WON", "agent_lost": "LOST", "tie": "TIE"}.get(latest.outcome, "PENDING")
        benchmark.update({
            "latest_outcome": outcome_word,
            "latest_outcome_label": _outcome_emoji(latest.outcome),
            "latest_lead_time_seconds": latest.time_delta_seconds,
            "latest_lead_time_human": _seconds_to_human(latest.time_delta_seconds),
            "latest_freshdesk_ticket_id": latest.freshdesk_ticket_id,
            "latest_incident_id": str(latest.incident_id)[:8],
        })
    else:
        alerted = [i for i in incidents if i.agent_alert_timestamp is not None]
        if alerted:
            benchmark["latest_incident_id"] = str(alerted[0].id)[:8]

    # ── Recent incidents table ───────────────────────────────────────────────
    recent_incidents = []
    for inc in incidents[:100]:
        m = match_by_incident.get(inc.id)
        recent_incidents.append({
            "incident_id": str(inc.id)[:8],
            "first_seen": inc.first_seen_at.isoformat() if inc.first_seen_at else None,
            "title": inc.title or inc.fingerprint,
            "severity": inc.severity,
            "score": inc.score,
            "status": inc.status,
            "business_action": inc.business_action,
            "endpoint": inc.endpoint,
            "country": inc.primary_country,
            "provider": inc.provider,
            "platform": inc.platform,
            "payment_method": inc.payment_method,
            "notification_status": inc.notification_status,
            "notification_channel": inc.notification_channel,
            "outcome": m.outcome if m else None,
            "outcome_label": _outcome_emoji(m.outcome if m else None),
            "lead_time_human": _seconds_to_human(m.time_delta_seconds) if m else "—",
            "confidence": round(m.confidence, 2) if (m and m.confidence is not None) else None,
        })

    # ── Breakdown by business action ─────────────────────────────────────────
    ba_groups: dict = {}
    for inc in incidents:
        key = inc.business_action or "unknown"
        g = ba_groups.setdefault(key, {
            "business_action": key, "incident_count": 0, "critical_count": 0,
            "delivered_alerts": 0, "freshdesk_matches": 0, "wins": 0, "losses": 0, "pending": 0,
        })
        g["incident_count"] += 1
        if inc.severity == "critical":
            g["critical_count"] += 1
        if inc.notification_status == "delivered":
            g["delivered_alerts"] += 1
        m = match_by_incident.get(inc.id)
        if m:
            g["freshdesk_matches"] += 1
            if m.outcome == "agent_won":
                g["wins"] += 1
            elif m.outcome == "agent_lost":
                g["losses"] += 1
        else:
            g["pending"] += 1
    by_business_action = sorted(ba_groups.values(), key=lambda g: g["incident_count"], reverse=True)

    # ── Breakdown by dimensions ──────────────────────────────────────────────
    def _dim_breakdown(attr: str):
        groups: dict = {}
        for inc in incidents:
            val = getattr(inc, attr, None)
            if not val:
                continue
            g = groups.setdefault(val, {
                "value": val, "incident_count": 0, "critical_count": 0, "delivered_alerts": 0,
            })
            g["incident_count"] += 1
            if inc.severity == "critical":
                g["critical_count"] += 1
            if inc.notification_status == "delivered":
                g["delivered_alerts"] += 1
        return sorted(groups.values(), key=lambda g: g["incident_count"], reverse=True)

    by_dimension = {
        "country": _dim_breakdown("primary_country"),
        "provider": _dim_breakdown("provider"),
        "platform": _dim_breakdown("platform"),
        "payment_method": _dim_breakdown("payment_method"),
    }

    # ── Audit highlights ─────────────────────────────────────────────────────
    def _highlight(inc):
        m = match_by_incident.get(inc.id)
        return {
            "incident_id": str(inc.id)[:8],
            "title": inc.title or inc.fingerprint,
            "business_action": inc.business_action,
            "first_seen": inc.first_seen_at.isoformat() if inc.first_seen_at else None,
            "notification_status": inc.notification_status,
            "freshdesk_ticket_id": m.freshdesk_ticket_id if m else None,
            "outcome": m.outcome if m else None,
        }

    audit_highlights = {
        "notification_failures": [_highlight(i) for i in incidents if i.notification_status == "failed"][:25],
        "alerted_not_matched": [
            _highlight(i) for i in incidents
            if i.agent_alert_timestamp is not None and i.id not in match_by_incident
        ][:25],
        "stale_unmatched": [
            _highlight(i) for i in incidents
            if i.first_seen_at is not None
            and _as_aware(i.first_seen_at) < stale_cutoff
            and i.id not in match_by_incident
        ][:25],
        "delivery_failed_but_ticket_appeared": [
            _highlight(i) for i in incidents
            if i.notification_status == "failed" and i.id in match_by_incident
        ][:25],
        "false_positives": [_highlight(i) for i in incidents if i.status == "false_positive"][:25],
    }

    # ── Source integration status (last 30 days) ─────────────────────────────
    async def _source_count(source: str) -> int:
        return (await db.execute(
            select(func.count(RawEvent.id))
            .where(RawEvent.source == source)
            .where(RawEvent.received_at >= since)
        )).scalar() or 0

    datadog_count = await _source_count("datadog")
    sentry_count = await _source_count("sentry")
    slack_count = sum(
        1 for i in incidents
        if i.notification_channel == "slack" and i.notification_status == "delivered"
    )

    sources = {
        "datadog": {"seen": datadog_count > 0, "count": datadog_count},
        "sentry": {"seen": sentry_count > 0, "count": sentry_count},
        "freshdesk": {
            "seen": tickets_seen > 0, "count": tickets_seen,
            "matched": len(matched_ticket_ids),
        },
        "slack": {"seen": slack_count > 0, "count": slack_count},
    }

    return {
        "generated_at": now.isoformat(),
        "window_days": WINDOW_DAYS,
        "since": since.isoformat(),
        "official_win_rule": OFFICIAL_WIN_RULE,
        "cards": {
            "total_incidents": total_incidents,
            "critical_incidents": critical_incidents,
            "alerts_delivered": alerts_delivered,
            "notification_failures": notification_failures,
            "freshdesk_matches": total_matched,
            "agent_wins": wins,
            "agent_losses": losses,
            "pending_unmatched": pending_unmatched,
            "win_rate_percent": win_rate,
            "median_lead_time_seconds": round(median_lead),
            "median_lead_time_human": _seconds_to_human(median_lead),
            "p90_lead_time_seconds": round(p90_lead),
            "p90_lead_time_human": _seconds_to_human(p90_lead),
            "active_anomalies": active_anomalies,
        },
        "benchmark": benchmark,
        "benchmark_summary": {
            "wins": wins,
            "losses": losses,
            "ties": ties,
            "pending_unmatched": pending_unmatched,
            "unmatched_freshdesk_tickets": unmatched_tickets,
            "win_rate_percent": win_rate,
            "median_lead_time_human": _seconds_to_human(median_lead),
            "p90_lead_time_human": _seconds_to_human(p90_lead),
            "notification_failures": notification_failures,
            "detected_before_support_but_delivery_failed": detected_failed,
        },
        "recent_incidents": recent_incidents,
        "anomalies": {"active_count": active_anomalies, "cells": metric_cells[:50]},
        "by_business_action": by_business_action,
        "by_dimension": by_dimension,
        "audit_highlights": audit_highlights,
        "sources": sources,
        "links": PRODUCTION_LINKS,
    }


def _as_aware(dt: datetime) -> datetime:
    """Coerce to UTC-aware (SQLite drops tzinfo on round-trip)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ─── Routes ───────────────────────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
async def dashboard_login_form(
    request: Request,
    error: Optional[str] = Query(default=None),
    eb_dashboard_session: Optional[str] = Cookie(default=None),
):
    """
    Browser login form. Lets an operator open the dashboard without ever putting
    DASHBOARD_API_KEY in the URL — the key is POSTed once and exchanged for a
    signed, HttpOnly session cookie. If already authenticated (or no key is
    configured), bounce straight to the dashboard.
    """
    if _is_authorized(None, eb_dashboard_session):
        return RedirectResponse(url="/dashboard/ui", status_code=303)
    return HTMLResponse(_login_html(error=error), status_code=200)


@router.post("/login")
async def dashboard_login_submit(
    request: Request,
    key: str = Form(...),
):
    """
    Validate the submitted key. On success, set the session cookie and redirect
    to /dashboard/ui (303 → the browser issues a clean GET, no key in the URL).
    On failure, re-render the form with an error and HTTP 401.
    """
    expected = settings.DASHBOARD_API_KEY
    if not expected:
        # No key configured → dashboard is open; nothing to authenticate.
        return RedirectResponse(url="/dashboard/ui", status_code=303)
    if hmac.compare_digest(key, expected):
        response = RedirectResponse(url="/dashboard/ui", status_code=303)
        _set_session_cookie(response, request, expected)
        return response
    return HTMLResponse(_login_html(error="Invalid dashboard key."), status_code=401)


@router.get("/logout")
async def dashboard_logout(request: Request):
    """Clear the session cookie and return to the login form."""
    response = RedirectResponse(url="/dashboard/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/dashboard")
    return response


@router.get("/data")
async def dashboard_data(
    db: AsyncSession = Depends(get_db),
    x_dashboard_key: Optional[str] = Header(default=None),
    key: Optional[str] = Query(default=None),
    eb_dashboard_session: Optional[str] = Cookie(default=None),
):
    """Consolidated JSON payload for the visual dashboard (last 30 days)."""
    if not _is_authorized(x_dashboard_key or key, eb_dashboard_session):
        return JSONResponse({"detail": "Invalid or missing dashboard key"}, status_code=401)
    data = await build_dashboard_data(db)
    return JSONResponse(data)


@router.get("/ui", response_class=HTMLResponse)
async def dashboard_ui(
    db: AsyncSession = Depends(get_db),
    x_dashboard_key: Optional[str] = Header(default=None),
    key: Optional[str] = Query(default=None),
    eb_dashboard_session: Optional[str] = Cookie(default=None),
):
    """Render the visual HTML dashboard (last 30 days)."""
    if not _is_authorized(x_dashboard_key or key, eb_dashboard_session):
        return HTMLResponse(_login_html(error=None), status_code=401)
    data = await build_dashboard_data(db)
    return HTMLResponse(render_html(data))


# ─── HTML rendering (server-side, inline CSS, no external deps) ───────────────

def _esc(value) -> str:
    """HTML-escape any value, rendering None as an em dash."""
    if value is None or value == "":
        return "—"
    return html.escape(str(value))


def _fmt_dt(iso: Optional[str]) -> str:
    """Render an ISO timestamp as compact 'YYYY-MM-DD HH:MM' UTC."""
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return _esc(iso)


def _pill(text: str, kind: str) -> str:
    """A colored status pill. kind ∈ {green, red, yellow, gray}."""
    return f'<span class="pill {kind}">{_esc(text)}</span>'


def _outcome_pill(outcome: Optional[str]) -> str:
    mapping = {
        "agent_won": ("WON", "green"),
        "agent_lost": ("LOST", "red"),
        "tie": ("TIE", "yellow"),
    }
    if outcome in mapping:
        label, kind = mapping[outcome]
        return _pill(label, kind)
    return _pill("PENDING", "yellow")


def _delivery_pill(status: Optional[str]) -> str:
    if status == "delivered":
        return _pill("delivered", "green")
    if status == "failed":
        return _pill("failed", "red")
    return _pill(status or "pending", "yellow")


def _severity_pill(sev: Optional[str]) -> str:
    kind = {"critical": "red", "high": "red", "medium": "yellow", "low": "gray"}.get(sev, "gray")
    return _pill(sev or "—", kind)


def _card(label: str, value, accent: str = "") -> str:
    cls = f"card {accent}".strip()
    return f'<div class="{cls}"><div class="card-value">{_esc(value)}</div><div class="card-label">{_esc(label)}</div></div>'


def _table(headers, rows, empty_msg: str) -> str:
    """Build an HTML table. `rows` is a list of pre-rendered cell-HTML lists."""
    if not rows:
        return f'<p class="empty">{_esc(empty_msg)}</p>'
    head = "".join(f"<th>{_esc(h)}</th>" for h in headers)
    body = ""
    for row in rows:
        body += "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>"
    return f'<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def render_html(data: dict) -> str:
    cards = data["cards"]
    bm = data["benchmark"]
    bs = data["benchmark_summary"]

    # Top status cards.
    cards_html = "".join([
        _card("Total incidents", cards["total_incidents"]),
        _card("Critical incidents", cards["critical_incidents"], "accent-red"),
        _card("Alerts delivered", cards["alerts_delivered"], "accent-green"),
        _card("Notification failures", cards["notification_failures"], "accent-red"),
        _card("Freshdesk matches", cards["freshdesk_matches"]),
        _card("Agent wins", cards["agent_wins"], "accent-green"),
        _card("Agent losses", cards["agent_losses"], "accent-red"),
        _card("Pending / unmatched", cards["pending_unmatched"], "accent-yellow"),
        _card("Win rate", f'{cards["win_rate_percent"]}%', "accent-green"),
        _card("Median lead time", cards["median_lead_time_human"]),
        _card("P90 lead time", cards["p90_lead_time_human"]),
        _card("Active anomalies", cards["active_anomalies"], "accent-yellow"),
    ])

    # Benchmark hero card.
    benchmark_html = f"""
    <div class="benchmark">
      <div class="benchmark-rule">Official rule: <code>{_esc(bm['official_rule'])}</code></div>
      <div class="benchmark-grid">
        <div><span class="muted">Latest outcome</span><div class="big">{_outcome_pill_from_word(bm['latest_outcome'])}</div></div>
        <div><span class="muted">Latest lead time</span><div class="big">{_esc(bm['latest_lead_time_human'])}</div></div>
        <div><span class="muted">Latest Freshdesk ticket</span><div class="big">{_esc(bm['latest_freshdesk_ticket_id'])}</div></div>
        <div><span class="muted">Latest incident</span><div class="big">{_esc(bm['latest_incident_id'])}</div></div>
      </div>
    </div>"""

    # Section 1 — Benchmark Summary.
    summary_rows = [
        ["Wins", str(bs["wins"])],
        ["Losses", str(bs["losses"])],
        ["Pending / unmatched", str(bs["pending_unmatched"])],
        ["Unmatched Freshdesk tickets", str(bs["unmatched_freshdesk_tickets"])],
        ["Win rate", f'{bs["win_rate_percent"]}%'],
        ["Median lead time", bs["median_lead_time_human"]],
        ["P90 lead time", bs["p90_lead_time_human"]],
        ["Notification failures", str(bs["notification_failures"])],
        ["Detected before support but delivery failed", str(bs["detected_before_support_but_delivery_failed"])],
    ]
    section1 = _table(["Metric", "Value"], [[_esc(k), _esc(v)] for k, v in summary_rows], "No benchmark data yet.")

    # Section 2 — Recent incidents.
    inc_headers = ["First seen", "Title", "Severity", "Score", "Status", "Business action",
                   "Endpoint", "Country", "Provider", "Platform", "Payment method",
                   "Notif. status", "Channel", "Outcome", "Lead time", "Confidence"]
    inc_rows = []
    for r in data["recent_incidents"]:
        inc_rows.append([
            _fmt_dt(r["first_seen"]), _esc(r["title"]), _severity_pill(r["severity"]),
            _esc(r["score"]), _esc(r["status"]), _esc(r["business_action"]),
            _esc(r["endpoint"]), _esc(r["country"]), _esc(r["provider"]),
            _esc(r["platform"]), _esc(r["payment_method"]),
            _delivery_pill(r["notification_status"]), _esc(r["notification_channel"]),
            _outcome_pill(r["outcome"]), _esc(r["lead_time_human"]), _esc(r["confidence"]),
        ])
    section2 = _table(inc_headers, inc_rows, "No incidents in the last 30 days.")

    # Section 3 — Active anomalies / metric buckets.
    anom_headers = ["Business action", "Dimension", "Value", "Current total", "Current success",
                    "Baseline total", "Baseline success", "Anomaly kind", "Severity boost"]
    anom_rows = []
    for c in data["anomalies"]["cells"]:
        anom_rows.append([
            _esc(c["business_action"]), _esc(c["dimension"]), _esc(c["dimension_value"]),
            _esc(c["current_total"]), _pct(c["current_success_rate"]),
            _esc(c["baseline_total"]), _pct(c["baseline_success_rate"]),
            _pill(c["anomaly_kind"], "red") if c["anomaly_kind"] else "—",
            _esc(c["severity_boost"]),
        ])
    section3 = _table(anom_headers, anom_rows, "No metric buckets yet.")

    # Section 4 — Breakdown by business action.
    ba_headers = ["Business action", "Incidents", "Critical", "Delivered", "FD matches", "Wins", "Losses", "Pending"]
    ba_rows = [[
        _esc(g["business_action"]), _esc(g["incident_count"]), _esc(g["critical_count"]),
        _esc(g["delivered_alerts"]), _esc(g["freshdesk_matches"]), _esc(g["wins"]),
        _esc(g["losses"]), _esc(g["pending"]),
    ] for g in data["by_business_action"]]
    section4 = _table(ba_headers, ba_rows, "No incidents in the last 30 days.")

    # Section 5 — Breakdown by dimensions.
    section5 = ""
    for dim_name in ("country", "provider", "platform", "payment_method"):
        rows = data["by_dimension"][dim_name]
        table = _table(
            [dim_name.replace("_", " ").title(), "Incidents", "Critical", "Delivered"],
            [[_esc(g["value"]), _esc(g["incident_count"]), _esc(g["critical_count"]), _esc(g["delivered_alerts"])] for g in rows],
            f"No {dim_name.replace('_', ' ')} data yet.",
        )
        section5 += f'<h3>{_esc(dim_name.replace("_", " ").title())}</h3>{table}'

    # Section 6 — Audit highlights.
    ah = data["audit_highlights"]
    section6 = ""
    audit_blocks = [
        ("Notification failures", ah["notification_failures"]),
        ("Alerted but not matched to Freshdesk", ah["alerted_not_matched"]),
        ("Incidents older than 24h with no match", ah["stale_unmatched"]),
        ("Delivery failed but Freshdesk ticket later appeared", ah["delivery_failed_but_ticket_appeared"]),
        ("False positives", ah["false_positives"]),
    ]
    for title, items in audit_blocks:
        rows = [[
            _esc(it["incident_id"]), _esc(it["title"]), _esc(it["business_action"]),
            _fmt_dt(it["first_seen"]), _delivery_pill(it["notification_status"]),
            _esc(it["freshdesk_ticket_id"]), _outcome_pill(it["outcome"]),
        ] for it in items]
        table = _table(
            ["Incident", "Title", "Business action", "First seen", "Notif.", "FD ticket", "Outcome"],
            rows, "None — all clear here.",
        )
        section6 += f'<h3>{_esc(title)}</h3>{table}'

    # Section 7 — Source integration status.
    src = data["sources"]
    def _src_pill(seen):
        return _pill("yes", "green") if seen else _pill("not observed yet", "gray")
    src_rows = [
        ["Datadog events", _src_pill(src["datadog"]["seen"]), _esc(src["datadog"]["count"])],
        ["Sentry events", _src_pill(src["sentry"]["seen"]), _esc(src["sentry"]["count"])],
        ["Freshdesk tickets", _src_pill(src["freshdesk"]["seen"]), f'{_esc(src["freshdesk"]["count"])} seen / {_esc(src["freshdesk"]["matched"])} matched'],
        ["Slack delivery", _src_pill(src["slack"]["seen"]), _esc(src["slack"]["count"])],
    ]
    section7 = _table(["Source", "Observed", "Count"], src_rows, "No source events observed yet.")

    # Section 8 — Production links (copy-friendly, no secrets).
    links = data["links"]
    link_items = ""
    link_labels = [
        ("Datadog webhook", links["datadog_webhook"]),
        ("Sentry webhook", links["sentry_webhook"]),
        ("Freshdesk webhook", links["freshdesk_webhook"]),
        ("Health", links["health"]),
        ("Readiness", links["ready"]),
    ]
    for label, url in link_labels:
        link_items += (
            f'<div class="link-row"><span class="link-label">{_esc(label)}</span>'
            f'<code class="link-url">{_esc(url)}</code>'
            f'<button class="copy-btn" data-url="{_esc(url)}">Copy</button></div>'
        )
    section8 = f'<div class="links">{link_items}</div>'

    generated = _fmt_dt(data["generated_at"])

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Earlybird Production Dashboard</title>
<style>
{_CSS}
</style>
</head>
<body>
<header>
  <a class="logout" href="/dashboard/logout">Sign out</a>
  <h1>Earlybird Production Dashboard</h1>
  <p class="subtitle">Last 30 days</p>
  <p class="generated">Generated {generated} UTC · read-only</p>
</header>

<section class="cards">{cards_html}</section>

<section>
  <h2>Most important benchmark</h2>
  {benchmark_html}
</section>

<section>
  <h2>1 · Benchmark Summary</h2>
  {section1}
</section>

<section>
  <h2>2 · Recent Incidents</h2>
  <div class="scroll-x">{section2}</div>
</section>

<section>
  <h2>3 · Active Anomalies / Metric Buckets</h2>
  <div class="scroll-x">{section3}</div>
</section>

<section>
  <h2>4 · Breakdown by Business Action</h2>
  <div class="scroll-x">{section4}</div>
</section>

<section>
  <h2>5 · Breakdown by Dimensions</h2>
  {section5}
</section>

<section>
  <h2>6 · Audit Highlights</h2>
  {section6}
</section>

<section>
  <h2>7 · Source Integration Status</h2>
  {section7}
</section>

<section>
  <h2>8 · Production Links</h2>
  {section8}
</section>

<footer>Earlybird · Official win rule: <code>{_esc(OFFICIAL_WIN_RULE)}</code></footer>

<script>
document.querySelectorAll('.copy-btn').forEach(function (btn) {{
  btn.addEventListener('click', function () {{
    var url = btn.getAttribute('data-url');
    navigator.clipboard.writeText(url).then(function () {{
      var prev = btn.textContent;
      btn.textContent = 'Copied!';
      setTimeout(function () {{ btn.textContent = prev; }}, 1200);
    }});
  }});
}});
</script>
</body>
</html>"""


def _outcome_pill_from_word(word: str) -> str:
    mapping = {"WON": "green", "LOST": "red", "TIE": "yellow", "PENDING": "yellow"}
    return _pill(word, mapping.get(word, "gray"))


def _pct(rate) -> str:
    if rate is None:
        return "—"
    return f"{round(rate * 100, 1)}%"


_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
       margin: 0; padding: 0 24px 64px; background: #f6f7f9; color: #1a1f24; line-height: 1.45; }
header { padding: 28px 0 12px; position: relative; }
h1 { margin: 0; font-size: 28px; }
.logout { position: absolute; top: 28px; right: 0; font-size: 13px; font-weight: 600;
          color: #2e6fd2; text-decoration: none; border: 1px solid #cfd6de;
          border-radius: 8px; padding: 6px 14px; }
.logout:hover { background: #eef2f8; }
.subtitle { margin: 4px 0 0; font-size: 16px; color: #5a6470; font-weight: 600; }
.generated { margin: 4px 0 0; font-size: 12px; color: #8a929c; }
h2 { font-size: 18px; margin: 32px 0 12px; border-bottom: 2px solid #e3e7ec; padding-bottom: 6px; }
h3 { font-size: 14px; margin: 18px 0 6px; color: #5a6470; }
section { max-width: 1400px; }
.cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 12px; margin-top: 16px; max-width: 1400px; }
.card { background: #fff; border: 1px solid #e3e7ec; border-radius: 10px; padding: 14px 16px; }
.card-value { font-size: 26px; font-weight: 700; }
.card-label { font-size: 12px; color: #6b7480; margin-top: 4px; }
.card.accent-green { border-left: 4px solid #2e9e5b; }
.card.accent-red { border-left: 4px solid #d23f3f; }
.card.accent-yellow { border-left: 4px solid #d9a521; }
.benchmark { background: #fff; border: 1px solid #e3e7ec; border-left: 5px solid #2e6fd2; border-radius: 12px; padding: 18px 20px; }
.benchmark-rule { font-size: 14px; margin-bottom: 14px; }
.benchmark-rule code { background: #eef2f8; padding: 3px 8px; border-radius: 6px; }
.benchmark-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 16px; }
.benchmark-grid .muted { font-size: 12px; color: #6b7480; }
.benchmark-grid .big { font-size: 20px; font-weight: 700; margin-top: 4px; }
table { border-collapse: collapse; width: 100%; background: #fff; border: 1px solid #e3e7ec; border-radius: 8px; overflow: hidden; font-size: 13px; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #eef1f4; white-space: nowrap; }
th { background: #f0f2f5; font-weight: 600; color: #41494f; }
tr:last-child td { border-bottom: none; }
.scroll-x { overflow-x: auto; }
.pill { display: inline-block; padding: 2px 9px; border-radius: 999px; font-size: 12px; font-weight: 600; }
.pill.green { background: #e3f6ea; color: #1d7a43; }
.pill.red { background: #fbe6e6; color: #b62b2b; }
.pill.yellow { background: #fbf3d9; color: #936a07; }
.pill.gray { background: #eceff2; color: #6b7480; }
.empty { color: #8a929c; font-style: italic; padding: 10px 0; }
.links { display: flex; flex-direction: column; gap: 8px; }
.link-row { display: flex; align-items: center; gap: 10px; background: #fff; border: 1px solid #e3e7ec; border-radius: 8px; padding: 8px 12px; flex-wrap: wrap; }
.link-label { font-weight: 600; min-width: 130px; }
.link-url { background: #eef2f8; padding: 4px 8px; border-radius: 6px; flex: 1; word-break: break-all; }
.copy-btn { border: 1px solid #cfd6de; background: #fff; border-radius: 6px; padding: 5px 12px; cursor: pointer; font-size: 12px; }
.copy-btn:hover { background: #f0f2f5; }
footer { max-width: 1400px; margin-top: 40px; padding-top: 16px; border-top: 1px solid #e3e7ec; font-size: 12px; color: #8a929c; }
footer code { background: #eef2f8; padding: 2px 6px; border-radius: 4px; }
@media (prefers-color-scheme: dark) {
  body { background: #14181d; color: #e6eaef; }
  .card, .benchmark, table, .link-row { background: #1c2128; border-color: #2a313a; }
  th { background: #232a32; color: #cdd5de; }
  td, th { border-color: #2a313a; }
  .card-label, .benchmark-grid .muted, .generated, .link-url, footer { color: #9aa4af; }
  .benchmark-rule code, .link-url, footer code { background: #232a32; }
  .copy-btn { background: #232a32; border-color: #3a434d; color: #e6eaef; }
  .logout { border-color: #3a434d; color: #6ea8fe; }
  .logout:hover { background: #232a32; }
}
"""


def _login_html(error: Optional[str] = None) -> str:
    """
    Browser login form. The key is POSTed to /dashboard/login and exchanged for
    a signed session cookie, so it never appears in the URL or browser history.
    Header (`x-dashboard-key`) and `?key=` access still work for debugging.
    """
    error_html = (
        f'<p class="error">{html.escape(error)}</p>' if error else ""
    )
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in — Earlybird Dashboard</title>
<style>
:root {{ color-scheme: light dark; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
       background: #f6f7f9; color: #1a1f24; display: flex; align-items: center;
       justify-content: center; min-height: 100vh; margin: 0; }}
.box {{ background: #fff; border: 1px solid #e3e7ec; border-left: 5px solid #2e6fd2;
       border-radius: 12px; padding: 32px 36px; width: 340px; max-width: 90vw; }}
h1 {{ margin: 0 0 4px; font-size: 20px; }}
.sub {{ color: #6b7480; margin: 0 0 20px; font-size: 13px; }}
label {{ display: block; font-size: 13px; font-weight: 600; margin-bottom: 6px; }}
input[type=password] {{ width: 100%; padding: 10px 12px; font-size: 14px;
       border: 1px solid #cfd6de; border-radius: 8px; background: #fff; color: inherit; }}
button {{ margin-top: 16px; width: 100%; padding: 10px 12px; font-size: 14px; font-weight: 600;
       border: none; border-radius: 8px; background: #2e6fd2; color: #fff; cursor: pointer; }}
button:hover {{ background: #2a63bd; }}
.error {{ color: #b62b2b; font-size: 13px; margin: 0 0 14px; }}
.hint {{ color: #8a929c; font-size: 12px; margin: 16px 0 0; }}
.hint code {{ background: #eef2f8; padding: 2px 6px; border-radius: 4px; }}
@media (prefers-color-scheme: dark) {{
  body {{ background: #14181d; color: #e6eaef; }}
  .box {{ background: #1c2128; border-color: #2a313a; }}
  input[type=password] {{ background: #232a32; border-color: #3a434d; }}
  .hint code {{ background: #232a32; }}
}}
</style></head>
<body><div class="box">
<h1>Earlybird Dashboard</h1>
<p class="sub">Sign in to view the last 30 days.</p>
{error_html}
<form method="post" action="/dashboard/login">
  <label for="key">Dashboard key</label>
  <input type="password" id="key" name="key" autocomplete="current-password" autofocus required>
  <button type="submit">Sign in</button>
</form>
<p class="hint">Your key is exchanged for a session cookie and never appears in the URL.
For debugging, the <code>x-dashboard-key</code> header and <code>?key=</code> query
parameter still work.</p>
</div></body></html>"""
