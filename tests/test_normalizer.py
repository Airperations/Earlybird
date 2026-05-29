import pytest

from app.normalizers.base import normalize_sentry, normalize, _build_fingerprint


SENTRY_PAYLOAD = {
    "project_slug": "payments-api",
    "event": {
        "title": "GatewayTimeout",
        "tags": [["environment", "production"], ["country_code", "MX"], ["http.status_code", "502"]],
        "request": {"url": "https://api.airdrive.com/api/v1/withdraw/confirm"},
        "exception": {"values": [{"type": "GatewayTimeout", "value": "no response"}]},
        "user": {"id": "user_mx_001"},
        "contexts": {"response": {"status_code": 502}},
        "platform": "python", "release": "v1.42.0",
        "timestamp": "2026-05-28T20:43:01Z",
    },
}


def test_sentry_normalization_core_fields():
    n = normalize_sentry(SENTRY_PAYLOAD)
    assert n.source == "sentry"
    assert n.service == "payments-api"
    assert n.endpoint == "/api/v1/withdraw/confirm"
    assert n.http_status == 502
    assert n.exception_type == "GatewayTimeout"
    assert n.country == "MX"
    assert n.user_id == "user_mx_001"
    assert n.event_timestamp is not None


def test_sentry_missing_fields_does_not_crash():
    n = normalize_sentry({"event": {}})       # no project, no request, no exception
    assert n.service == "unknown"
    assert n.endpoint == ""
    assert n.http_status is None


def test_router_rejects_unknown_source():
    with pytest.raises(ValueError):
        normalize("pagerduty", {})


def test_fingerprint_is_stable_short_and_discriminating():
    fp1 = _build_fingerprint("payments-api", "/withdraw", 502, "GatewayTimeout")
    fp2 = _build_fingerprint("payments-api", "/withdraw", 502, "GatewayTimeout")
    fp3 = _build_fingerprint("payments-api", "/deposit", 502, "GatewayTimeout")
    assert fp1 == fp2          # same inputs -> same fingerprint (dedup works)
    assert fp1 != fp3          # different endpoint -> different fingerprint
    assert len(fp1) == 16
