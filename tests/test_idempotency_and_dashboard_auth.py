import pytest
from fastapi import HTTPException

from app.workers.process_event import _idempotency_key
from app.dashboard.routes import require_dashboard_key
from app.config import settings


def test_idempotency_key_is_deterministic():
    a = _idempotency_key("sentry", "2026-05-28T20:43:01+00:00", {"b": 1, "a": 2})
    # Same inputs (even with keys in different order) -> same key.
    b = _idempotency_key("sentry", "2026-05-28T20:43:01+00:00", {"a": 2, "b": 1})
    assert a == b and len(a) == 64


def test_idempotency_key_changes_with_payload():
    a = _idempotency_key("sentry", "2026-05-28T20:43:01+00:00", {"x": 1})
    b = _idempotency_key("sentry", "2026-05-28T20:43:01+00:00", {"x": 2})
    c = _idempotency_key("datadog", "2026-05-28T20:43:01+00:00", {"x": 1})
    assert a != b and a != c


def test_dashboard_open_when_no_key(monkeypatch):
    monkeypatch.setattr(settings, "DASHBOARD_API_KEY", None)
    require_dashboard_key(x_dashboard_key=None)  # no raise


def test_dashboard_rejects_without_key(monkeypatch):
    monkeypatch.setattr(settings, "DASHBOARD_API_KEY", "topsecret")
    with pytest.raises(HTTPException) as exc:
        require_dashboard_key(x_dashboard_key=None)
    assert exc.value.status_code == 401


def test_dashboard_accepts_correct_key(monkeypatch):
    monkeypatch.setattr(settings, "DASHBOARD_API_KEY", "topsecret")
    require_dashboard_key(x_dashboard_key="topsecret")  # no raise
