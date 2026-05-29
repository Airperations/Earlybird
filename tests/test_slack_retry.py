"""Tests for Slack delivery retry / backoff behavior."""

import pytest
import requests

from app.alerts import slack
from app.config import settings


class _FakeResp:
    def __init__(self, ok=True):
        self._ok = ok

    def raise_for_status(self):
        if not self._ok:
            raise requests.RequestException("boom")

    def json(self):
        return {"ok": True, "ts": "1700000000.000100", "channel": "C1"}


@pytest.fixture(autouse=True)
def _fast_slack(monkeypatch):
    # Use the webhook transport (no bot token) and zero out backoff sleeps.
    monkeypatch.setattr(settings, "SLACK_BOT_TOKEN", "", raising=False)
    monkeypatch.setattr(settings, "SLACK_WEBHOOK_URL", "https://hooks.slack.test/x", raising=False)
    monkeypatch.setattr(settings, "SLACK_MAX_RETRIES", 3, raising=False)
    monkeypatch.setattr(settings, "SLACK_RETRY_BACKOFF_SECONDS", 0.0, raising=False)
    monkeypatch.setattr(slack.time, "sleep", lambda *_a, **_k: None)


def test_succeeds_after_transient_failures(monkeypatch):
    calls = {"n": 0}

    def flaky_post(*args, **kwargs):
        calls["n"] += 1
        # Fail the first two attempts, succeed on the third.
        return _FakeResp(ok=calls["n"] >= 3)

    monkeypatch.setattr(slack.requests, "post", flaky_post)

    result = slack.send_immediate_alert(
        incident_id="abc", fingerprint="fp", severity="critical", score=125,
        affected_users=3, event_count=5, countries=["MX"],
        endpoint="/withdraw", service="payments-api",
    )

    assert result.delivered is True
    assert result.attempts == 3
    assert calls["n"] == 3


def test_fails_after_exhausting_retries(monkeypatch):
    calls = {"n": 0}

    def always_fail(*args, **kwargs):
        calls["n"] += 1
        return _FakeResp(ok=False)

    monkeypatch.setattr(slack.requests, "post", always_fail)

    result = slack.send_immediate_alert(
        incident_id="abc", fingerprint="fp", severity="critical", score=125,
        affected_users=3, event_count=5, countries=["MX"],
        endpoint="/withdraw", service="payments-api",
    )

    assert result.delivered is False
    assert result.attempts == 3
    assert calls["n"] == 3
    assert result.error is not None


def test_not_configured_returns_failure(monkeypatch):
    monkeypatch.setattr(settings, "SLACK_WEBHOOK_URL", "", raising=False)
    monkeypatch.setattr(settings, "SLACK_BOT_TOKEN", "", raising=False)

    result = slack.send_immediate_alert(
        incident_id="abc", fingerprint="fp", severity="low", score=40,
        affected_users=1, event_count=1, countries=[],
        endpoint="/x", service="svc",
    )
    assert result.delivered is False
    assert result.attempts == 0
    assert result.error == "not_configured"


def test_first_alert_has_no_llm_text(monkeypatch):
    captured = {}

    def capture_post(url, json=None, **kwargs):
        captured["payload"] = json
        return _FakeResp(ok=True)

    monkeypatch.setattr(slack.requests, "post", capture_post)

    slack.send_immediate_alert(
        incident_id="abc", fingerprint="fp", severity="critical", score=125,
        affected_users=3, event_count=5, countries=["MX"],
        endpoint="/withdraw", service="payments-api", status="enriching…",
    )

    blob = str(captured["payload"]).lower()
    # The minimal alert must advertise that AI analysis is still pending,
    # and must not contain a root-cause / summary section.
    assert "enriching" in blob
    assert "suspected root cause" not in blob
