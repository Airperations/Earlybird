"""Tests that incidents carry structured business metadata (no LLM needed)."""

import pytest

from app.incidents import service
from tests.conftest import make_normalized


@pytest.mark.asyncio
async def test_new_incident_gets_structured_metadata(db):
    n = make_normalized(
        fingerprint="fp-meta", service="payments-api", endpoint="/withdraw/confirm",
        http_status=502, exception_type="GatewayTimeout", country="MX",
        platform="ios", provider="stripe", payment_method="card",
        business_action="withdrawal_failed",
    )
    inc = await service.find_or_create_incident(db, n)
    await db.flush()

    assert inc.service == "payments-api"
    assert inc.endpoint == "/withdraw/confirm"
    assert inc.route == "/withdraw/confirm"
    assert inc.business_action == "withdrawal_failed"
    assert inc.http_status == 502
    assert inc.exception_type == "GatewayTimeout"
    assert inc.primary_country == "MX"
    assert inc.platform == "ios"
    assert inc.provider == "stripe"
    assert inc.payment_method == "card"
    # normalized_keywords carries the searchable footprint (EN + ES synonyms).
    assert "retiro" in inc.normalized_keywords
    assert "withdraw" in inc.normalized_keywords
    assert "stripe" in inc.normalized_keywords


@pytest.mark.asyncio
async def test_metadata_backfilled_from_later_event(db):
    first = make_normalized(fingerprint="fp-backfill", provider=None, payment_method=None)
    inc = await service.find_or_create_incident(db, first)
    await db.flush()
    assert inc.provider is None

    later = make_normalized(fingerprint="fp-backfill", provider="stripe", payment_method="crypto")
    await service.find_or_create_incident(db, later)
    await db.flush()

    assert inc.provider == "stripe"          # backfilled, not overwritten with None earlier
    assert inc.payment_method == "crypto"
    assert "stripe" in inc.normalized_keywords
