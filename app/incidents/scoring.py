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
    user_score = score_affected_users(affected_users)
    velocity_score = score_error_velocity(
        events_in_window if events_in_window is not None else event_count
    )
    status_score = score_http_status(event.http_status)
    country_score = score_country_concentration(countries or [])
    freshdesk_score = score_freshdesk_awareness(has_existing_tickets)

    total = (
        path_score
        + user_score
        + velocity_score
        + status_score
        + country_score
        + freshdesk_score
    )

    breakdown = {
        "critical_path": path_score,
        "affected_users": user_score,
        "error_velocity": velocity_score,
        "http_status": status_score,
        "country_concentration": country_score,
        "freshdesk_awareness": freshdesk_score,
    }

    severity = _score_to_severity(total)

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
