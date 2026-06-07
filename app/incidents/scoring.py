"""
Earlybird — Business Criticality Scoring Matrix
Scores each incident based on business impact, not just technical severity.
Critical paths are loaded from YAML config for easy tuning.
"""

import yaml
import os
from typing import Optional
from dataclasses import dataclass
from app.normalizers.base import NormalizedEventSchema
from app.taxonomy import base_action

# ─── Critical Paths Config ───────────────────────────────────────────────────

CRITICAL_PATHS_FILE = os.path.join(os.path.dirname(__file__), "../../config/critical_paths.yaml")

_CRITICAL_PATHS: dict = {}


def _load_critical_paths() -> dict:
    global _CRITICAL_PATHS
    if _CRITICAL_PATHS:
        return _CRITICAL_PATHS
    try:
        with open(CRITICAL_PATHS_FILE) as f:
            data = yaml.safe_load(f)
            _CRITICAL_PATHS = {p["path"]: p for p in data.get("critical_paths", [])}
    except FileNotFoundError:
        # Default critical paths if YAML not found
        _CRITICAL_PATHS = {
            "/withdraw": {"path": "/withdraw", "score": 50, "owner": "payments"},
            "/deposit": {"path": "/deposit", "score": 50, "owner": "payments"},
            "/p2p": {"path": "/p2p", "score": 45, "owner": "marketplace"},
            "/crypto": {"path": "/crypto", "score": 45, "owner": "crypto"},
            "/auth": {"path": "/auth", "score": 40, "owner": "identity"},
            "/kyc": {"path": "/kyc", "score": 35, "owner": "compliance"},
        }
    return _CRITICAL_PATHS


# ─── Scoring Functions ────────────────────────────────────────────────────────

def score_critical_path(endpoint: str) -> tuple[int, Optional[str]]:
    """Score based on business criticality of the affected endpoint."""
    paths = _load_critical_paths()
    for path, config in paths.items():
        if endpoint and path in endpoint:
            return config.get("score", 0), config.get("owner")
    return 0, None


def score_affected_users(count: int) -> int:
    """Score based on number of unique users affected."""
    if count >= 100:
        return 60
    elif count >= 21:
        return 45
    elif count >= 6:
        return 30
    elif count >= 2:
        return 15
    elif count == 1:
        return 5
    return 0


def score_error_velocity(events_in_window: int, window_minutes: int = 5) -> int:
    """
    Score based on error rate (errors per minute) within a recent window.

    `events_in_window` should be the count of events seen in the last
    `window_minutes` minutes — a true sliding window, not a cumulative total.
    The caller supplies this from Redis (see workers.process_event); when no
    windowed count is available it falls back to the incident's event_count.
    """
    rate = events_in_window / max(window_minutes, 1)
    if rate >= 20:
        return 60
    elif rate >= 10:
        return 40
    elif rate >= 5:
        return 20
    elif rate >= 2:
        return 10
    return 0


def score_http_status(status: Optional[int]) -> int:
    """Score based on HTTP status code severity."""
    if status is None:
        return 0
    if 500 <= status <= 599:
        return 30
    if status == 408 or status == 504:  # Timeout
        return 30
    if status == 402:                   # Payment required / failed
        return 35
    if status == 401 or status == 403:  # Auth failures
        return 30
    if 400 <= status <= 499:
        return 10
    return 0


def score_country_concentration(countries: list) -> int:
    """Score if errors are concentrated in specific region."""
    if not countries:
        return 0
    # LATAM key markets for Airdrive
    key_markets = {"MX", "CO", "AR", "VE", "PE", "CL", "BR", "EC"}
    if any(c in key_markets for c in countries):
        return 20
    return 10


def score_freshdesk_awareness(has_existing_tickets: bool) -> int:
    """Bonus for alerting before any ticket exists."""
    return 20 if not has_existing_tickets else 5


# Business actions that ride no HTTP endpoint (so score_critical_path can't see
# them) but ARE high-impact money/transaction flows. Stellar transaction
# build/submit lag delays user funds at the settlement layer. Keyed by
# base_action (see app.taxonomy). Existing endpoint-scored actions are absent
# here, so endpoint-based scoring is never double-counted.
_BUSINESS_ACTION_PATHS = {
    "stellar": {"score": 45, "owner": "stellar"},
}


def score_business_action_path(business_action: Optional[str]) -> tuple[int, Optional[str]]:
    """
    Critical-path score for actions identified by business_action rather than by
    URL endpoint. Returns (0, None) for everything else — including every action
    that already scores via score_critical_path — so existing scores are unchanged.
    """
    cfg = _BUSINESS_ACTION_PATHS.get(base_action(business_action))
    if cfg:
        return cfg["score"], cfg["owner"]
    return 0, None


def score_log_level(level: Optional[str]) -> int:
    """
    Score the structured-log level (Datadog log events). Only critical/fatal logs
    add points; ``error`` and below add nothing here (other signals already cover
    them). Returns 0 when no level was reported, so monitor/Sentry payloads — which
    never set `level` — are unaffected.
    """
    if not level:
        return 0
    if str(level).lower() in ("critical", "fatal", "emergency", "alert"):
        return 40
    return 0


def score_retry_storm(attempts: Optional[int], message: Optional[str]) -> int:
    """
    Score a retry storm / message-store lag: a very high attempt count or an
    explicit "retried too many times" signal means user transactions are wedged.
    Returns 0 when neither is present, so existing payloads (no `attempts`, no such
    message) are unaffected.
    """
    score = 0
    a = attempts or 0
    if a >= 500:
        score = 35
    elif a >= 100:
        score = 25
    elif a >= 25:
        score = 15
    elif a >= 10:
        score = 10
    msg = (message or "").lower()
    if "retried too many times" in msg or "too many times" in msg or "max retries" in msg:
        score = max(score, 30)
    return score


# ─── Main Scorer ──────────────────────────────────────────────────────────────

@dataclass
class ScoringResult:
    total_score: int
    severity: str
    breakdown: dict
    suggested_owner: Optional[str]
    # True when the incident hit a business-critical endpoint (e.g. /withdraw).
    # Such incidents alert at a lower threshold — a low-volume but high-impact
    # financial failure must still beat support.
    is_critical_path: bool = False

    def should_alert(self, threshold: int = 60) -> bool:
        return self.total_score >= threshold


def calculate_criticality(
    event: NormalizedEventSchema,
    affected_users: int = 1,
    event_count: int = 1,
    countries: list = None,
    has_existing_tickets: bool = False,
    events_in_window: Optional[int] = None,
) -> ScoringResult:
    """
    Calculate the full business criticality score for an incident.
    Returns a ScoringResult with breakdown for transparency.

    `events_in_window` is the count of events in the recent sliding window
    (errors/min basis). When None we fall back to the cumulative event_count.
    """
    path_score, owner = score_critical_path(event.endpoint)
    # A high-impact action that rides no HTTP endpoint (e.g. Stellar transaction
    # lag) is treated as a critical path too — only when the endpoint didn't
    # already match one, so existing endpoint-scored incidents never change.
    if path_score == 0:
        ba_score, ba_owner = score_business_action_path(event.business_action)
        if ba_score:
            path_score, owner = ba_score, ba_owner
    user_score = score_affected_users(affected_users)
    velocity_score = score_error_velocity(
        events_in_window if events_in_window is not None else event_count
    )
    status_score = score_http_status(event.http_status)
    country_score = score_country_concentration(countries or [])
    freshdesk_score = score_freshdesk_awareness(has_existing_tickets)
    # New structured-log signals. Both return 0 for monitor/Sentry/product
    # payloads (which carry neither `level` nor `attempts`), so all existing
    # scores are byte-for-byte unchanged.
    log_level_score = score_log_level(getattr(event, "level", None))
    retry_score = score_retry_storm(getattr(event, "attempts", None), event.message)

    total = (
        path_score
        + user_score
        + velocity_score
        + status_score
        + country_score
        + freshdesk_score
        + log_level_score
        + retry_score
    )

    breakdown = {
        "critical_path": path_score,
        "affected_users": user_score,
        "error_velocity": velocity_score,
        "http_status": status_score,
        "country_concentration": country_score,
        "freshdesk_awareness": freshdesk_score,
        "log_level": log_level_score,
        "retry_storm": retry_score,
    }

    severity = _score_to_severity(total)
    # A critical/fatal log level must surface as at least high severity even if the
    # numeric total lands lower. Scoped to events that report level=critical, so
    # nothing else is affected.
    if getattr(event, "level", None) and str(event.level).lower() in ("critical", "fatal"):
        if severity not in ("critical", "high"):
            severity = "high"

    return ScoringResult(
        total_score=total,
        severity=severity,
        breakdown=breakdown,
        suggested_owner=owner,
        is_critical_path=path_score > 0,
    )


def _score_to_severity(score: int) -> str:
    if score >= 100:
        return "critical"
    elif score >= 80:
        return "high"
    elif score >= 60:
        return "medium"
    elif score >= 40:
        return "low"
    return "observe"
