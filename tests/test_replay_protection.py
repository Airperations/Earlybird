import time
import pytest
from fastapi import HTTPException

from app.webhooks import security


class _FakeRequest:
    def __init__(self, headers):
        self.headers = headers


class _FakeRedis:
    """Minimal async stand-in: SET NX behavior over an in-memory dict."""
    def __init__(self):
        self.store = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True


@pytest.mark.asyncio
async def test_no_headers_is_allowed():
    await security.enforce_replay_protection(_FakeRequest({}), "datadog", redis_client=_FakeRedis())


@pytest.mark.asyncio
async def test_fresh_timestamp_allowed():
    req = _FakeRequest({"x-webhook-timestamp": str(time.time())})
    await security.enforce_replay_protection(req, "datadog", redis_client=_FakeRedis())


@pytest.mark.asyncio
async def test_stale_timestamp_rejected():
    req = _FakeRequest({"x-webhook-timestamp": str(time.time() - 9999)})
    with pytest.raises(HTTPException) as exc:
        await security.enforce_replay_protection(req, "datadog", redis_client=_FakeRedis())
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_invalid_timestamp_is_400():
    req = _FakeRequest({"x-webhook-timestamp": "not-a-time"})
    with pytest.raises(HTTPException) as exc:
        await security.enforce_replay_protection(req, "datadog", redis_client=_FakeRedis())
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_nonce_single_use():
    r = _FakeRedis()
    req = _FakeRequest({"x-webhook-nonce": "abc123"})
    await security.enforce_replay_protection(req, "freshdesk", redis_client=r)  # first use ok
    with pytest.raises(HTTPException) as exc:
        await security.enforce_replay_protection(req, "freshdesk", redis_client=r)  # replay
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_same_nonce_different_source_is_independent():
    r = _FakeRedis()
    await security.enforce_replay_protection(_FakeRequest({"x-webhook-nonce": "n"}), "datadog", redis_client=r)
    # Same nonce value under a different source namespace is fine.
    await security.enforce_replay_protection(_FakeRequest({"x-webhook-nonce": "n"}), "product", redis_client=r)
