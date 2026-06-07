"""
Tests for the visual production dashboard (GET /dashboard/ui and /dashboard/data).

These run the real FastAPI app over httpx's ASGI transport, with the async
SQLite `db` fixture injected in place of the Postgres session. They verify auth
(header + ?key=), the 30-day window, empty-database behavior, and that no raw
user identifiers, webhook secrets, or the dashboard key ever leak into output.
"""

import uuid
from datetime import datetime, timezone, timedelta

import httpx
import pytest

from app.config import settings
from app.database import get_db
from app.main import app
from app.models import Incident, FreshdeskTicket, IncidentFreshdeskMatch, RawEvent

KEY = "test-dashboard-key"


@pytest.fixture
def client(db, monkeypatch):
    """An ASGI client wired to the in-memory `db` session, with a key required."""
    monkeypatch.setattr(settings, "DASHBOARD_API_KEY", KEY)

    async def _override_get_db():
        yield db

    app.dependency_overrides[get_db] = _override_get_db
    transport = httpx.ASGITransport(app=app)
    yield httpx.AsyncClient(transport=transport, base_url="http://test")
    app.dependency_overrides.pop(get_db, None)


def _incident(status="alerted", days_ago=1, **kw):
    seen = datetime.now(timezone.utc) - timedelta(days=days_ago)
    base = dict(
        id=uuid.uuid4(), fingerprint=f"fp-{uuid.uuid4().hex[:6]}", status=status,
        first_seen_at=seen, last_seen_at=seen, severity="critical", score=120,
        business_action="withdrawal_failed", endpoint="/withdraw/confirm",
        primary_country="MX", provider="stripe", platform="ios", payment_method="card",
        notification_status="delivered", notification_channel="slack",
        affected_user_hashes=["hashed_user_secret_xyz"],
    )
    base.update(kw)
    return Incident(**base)


@pytest.mark.asyncio
async def test_ui_without_key_returns_401(client):
    async with client:
        resp = await client.get("/dashboard/ui")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_ui_with_valid_header_key_returns_200(client):
    async with client:
        resp = await client.get("/dashboard/ui", headers={"x-dashboard-key": KEY})
    assert resp.status_code == 200
    assert "Earlybird Production Dashboard" in resp.text
    assert "Last 30 days" in resp.text


@pytest.mark.asyncio
async def test_ui_with_query_param_key_returns_200(client):
    """Browser-friendly ?key= access works."""
    async with client:
        resp = await client.get(f"/dashboard/ui?key={KEY}")
    assert resp.status_code == 200
    assert "Earlybird Production Dashboard" in resp.text


@pytest.mark.asyncio
async def test_data_with_valid_key_returns_json(client):
    async with client:
        resp = await client.get("/dashboard/data", headers={"x-dashboard-key": KEY})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    data = resp.json()
    assert "cards" in data
    assert "benchmark" in data
    assert data["window_days"] == 30
    assert data["official_win_rule"] == "agent_alert_timestamp < freshdesk_ticket_created_at"


@pytest.mark.asyncio
async def test_data_without_key_returns_401(client):
    async with client:
        resp = await client.get("/dashboard/data")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_includes_incidents_from_last_30_days(client, db):
    inc = _incident(days_ago=2, title="Recent withdrawal spike")
    db.add(inc)
    await db.flush()

    async with client:
        resp = await client.get(f"/dashboard/data?key={KEY}")
    data = resp.json()
    assert data["cards"]["total_incidents"] == 1
    titles = [r["title"] for r in data["recent_incidents"]]
    assert "Recent withdrawal spike" in titles


@pytest.mark.asyncio
async def test_excludes_incidents_older_than_30_days(client, db):
    recent = _incident(days_ago=3, title="Recent one")
    old = _incident(days_ago=40, title="Ancient one")
    db.add_all([recent, old])
    await db.flush()

    async with client:
        resp = await client.get(f"/dashboard/data?key={KEY}")
    data = resp.json()
    assert data["cards"]["total_incidents"] == 1
    titles = [r["title"] for r in data["recent_incidents"]]
    assert "Recent one" in titles
    assert "Ancient one" not in titles


@pytest.mark.asyncio
async def test_works_with_empty_database(client):
    async with client:
        json_resp = await client.get(f"/dashboard/data?key={KEY}")
        ui_resp = await client.get(f"/dashboard/ui?key={KEY}")
    assert json_resp.status_code == 200
    data = json_resp.json()
    assert data["cards"]["total_incidents"] == 0
    assert data["cards"]["win_rate_percent"] == 0.0
    assert ui_resp.status_code == 200
    assert "No incidents in the last 30 days." in ui_resp.text


@pytest.mark.asyncio
async def test_does_not_expose_raw_user_ids(client, db):
    inc = _incident(days_ago=1)
    db.add(inc)
    await db.flush()

    async with client:
        resp = await client.get(f"/dashboard/data?key={KEY}")
        ui = await client.get(f"/dashboard/ui?key={KEY}")
    # The salted user hash stored on the incident must never reach the dashboard.
    assert "hashed_user_secret_xyz" not in resp.text
    assert "hashed_user_secret_xyz" not in ui.text
    assert "affected_user_hashes" not in resp.text


@pytest.mark.asyncio
async def test_does_not_expose_secrets_or_dashboard_key(client, db, monkeypatch):
    monkeypatch.setattr(settings, "SLACK_BOT_TOKEN", "xoxb-super-secret-token")
    monkeypatch.setattr(settings, "FRESHDESK_API_KEY", "freshdesk-secret-key")
    monkeypatch.setattr(settings, "SENTRY_WEBHOOK_SECRET", "sentry-webhook-secret")

    inc = _incident(days_ago=1)
    db.add(inc)
    await db.flush()

    async with client:
        data = await client.get(f"/dashboard/data?key={KEY}")
        ui = await client.get(f"/dashboard/ui?key={KEY}")

    for blob in (data.text, ui.text):
        assert KEY not in blob
        assert "xoxb-super-secret-token" not in blob
        assert "freshdesk-secret-key" not in blob
        assert "sentry-webhook-secret" not in blob


@pytest.mark.asyncio
async def test_login_form_renders(client):
    """GET /dashboard/login serves the browser sign-in form (no key needed)."""
    async with client:
        resp = await client.get("/dashboard/login")
    assert resp.status_code == 200
    assert "<form" in resp.text
    assert 'action="/dashboard/login"' in resp.text
    assert 'name="key"' in resp.text


@pytest.mark.asyncio
async def test_login_with_correct_key_sets_cookie_and_redirects(client):
    """POST with the right key sets an HttpOnly session cookie and redirects to /ui."""
    async with client:
        resp = await client.post("/dashboard/login", data={"key": KEY})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard/ui"
    set_cookie = resp.headers.get("set-cookie", "")
    assert "eb_dashboard_session=" in set_cookie
    assert "HttpOnly" in set_cookie
    # The raw key must never appear in the cookie value.
    assert KEY not in set_cookie


@pytest.mark.asyncio
async def test_login_with_wrong_key_returns_401_no_cookie(client):
    async with client:
        resp = await client.post("/dashboard/login", data={"key": "wrong"})
    assert resp.status_code == 401
    assert "Invalid dashboard key" in resp.text
    assert "set-cookie" not in resp.headers


@pytest.mark.asyncio
async def test_session_cookie_grants_access_without_key_in_url(client):
    """The login cookie alone opens /ui and /data — no ?key= or header needed."""
    async with client:
        # The client persists the Set-Cookie from login, like a real browser.
        await client.post("/dashboard/login", data={"key": KEY})
        ui = await client.get("/dashboard/ui")
        data = await client.get("/dashboard/data")
    assert ui.status_code == 200
    assert "Earlybird Production Dashboard" in ui.text
    assert data.status_code == 200
    assert "cards" in data.json()


@pytest.mark.asyncio
async def test_ui_unauthorized_shows_login_form(client):
    """An unauthenticated /ui still 401s, but now offers the login form."""
    async with client:
        resp = await client.get("/dashboard/ui")
    assert resp.status_code == 401
    assert 'action="/dashboard/login"' in resp.text


@pytest.mark.asyncio
async def test_logout_clears_cookie(client):
    async with client:
        resp = await client.get("/dashboard/logout")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard/login"
    assert "eb_dashboard_session=" in resp.headers.get("set-cookie", "")


@pytest.mark.asyncio
async def test_tampered_session_cookie_is_rejected(client):
    async with client:
        client.cookies.set("eb_dashboard_session", "deadbeef")
        resp = await client.get("/dashboard/data")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_benchmark_and_sources_reflect_data(client, db):
    """A won incident + ticket + Datadog event should surface in the payload."""
    alert_ts = datetime.now(timezone.utc) - timedelta(days=1)
    ticket_ts = alert_ts + timedelta(minutes=4, seconds=28)

    inc = _incident(days_ago=1, status="matched_to_freshdesk",
                    agent_alert_timestamp=alert_ts, notification_delivered_at=alert_ts,
                    detected_at=alert_ts)
    db.add(inc)
    ticket = FreshdeskTicket(id="FD-100", subject="withdrawal stuck", created_at=ticket_ts)
    db.add(ticket)
    raw = RawEvent(id=uuid.uuid4(), source="datadog", received_at=alert_ts,
                   raw_payload={"monitor": "withdrawals"})
    db.add(raw)
    await db.flush()
    db.add(IncidentFreshdeskMatch(
        id=uuid.uuid4(), incident_id=inc.id, freshdesk_ticket_id="FD-100",
        agent_alert_timestamp=alert_ts, freshdesk_ticket_timestamp=ticket_ts,
        time_delta_seconds=268, outcome="agent_won", confidence=0.92,
    ))
    await db.flush()

    async with client:
        resp = await client.get(f"/dashboard/data?key={KEY}")
    data = resp.json()
    assert data["cards"]["agent_wins"] == 1
    assert data["cards"]["win_rate_percent"] == 100.0
    assert data["benchmark"]["latest_outcome"] == "WON"
    assert data["benchmark"]["latest_freshdesk_ticket_id"] == "FD-100"
    assert data["sources"]["datadog"]["seen"] is True
    assert data["sources"]["datadog"]["count"] == 1
    assert data["sources"]["freshdesk"]["matched"] == 1
