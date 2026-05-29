from app.normalizers.base import NormalizedEventSchema
from app.incidents import scoring


def _event(endpoint="/withdraw/confirm", http_status=502):
    return NormalizedEventSchema(
        source="sentry", service="payments-api", environment="production",
        endpoint=endpoint, url="https://api/x", http_status=http_status,
        exception_type="GatewayTimeout", message="boom", user_id="u1",
        country="MX", platform="python", release="v1", fingerprint="abc", raw_payload={},
    )


def test_critical_path_scored():
    pts, owner = scoring.score_critical_path("/api/v1/withdraw/confirm")
    assert pts == 50 and owner == "payments"


def test_non_critical_path():
    assert scoring.score_critical_path("/api/v1/profile/avatar")[0] == 10  # /profile = 10
    assert scoring.score_critical_path("/healthz")[0] == 0


def test_http_status_buckets():
    assert scoring.score_http_status(502) == 30
    assert scoring.score_http_status(402) == 35
    assert scoring.score_http_status(200) == 0
    assert scoring.score_http_status(None) == 0


def test_affected_users_tiers():
    assert scoring.score_affected_users(100) == 60
    assert scoring.score_affected_users(21) == 45
    assert scoring.score_affected_users(1) == 5
    assert scoring.score_affected_users(0) == 0


def test_country_concentration_latam():
    assert scoring.score_country_concentration(["MX"]) == 20
    assert scoring.score_country_concentration(["DE"]) == 10
    assert scoring.score_country_concentration([]) == 0


def test_full_score_demo_scenario_is_125_and_critical():
    # Mirrors simulate_demo.py: /withdraw + MX + 502, 1 user, no ticket.
    result = scoring.calculate_criticality(
        _event(), affected_users=1, event_count=1, countries=["MX"], has_existing_tickets=False,
    )
    # 50 + 5 + 0 + 30 + 20 + 20 = 125  (the README's old "~150" was wrong)
    assert result.total_score == 125
    assert result.severity == "critical"
    assert result.breakdown["critical_path"] == 50


def test_high_user_impact_now_reachable():
    # Regression guard for audit fix C4: a wide outbreak must score the +60 tier.
    result = scoring.calculate_criticality(
        _event(), affected_users=150, event_count=1, countries=["MX"],
    )
    assert result.breakdown["affected_users"] == 60


def test_severity_thresholds():
    assert scoring._score_to_severity(39) == "observe"
    assert scoring._score_to_severity(40) == "low"
    assert scoring._score_to_severity(60) == "medium"
    assert scoring._score_to_severity(80) == "high"
    assert scoring._score_to_severity(100) == "critical"
