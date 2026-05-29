import pytest
from fastapi import HTTPException

from app.webhooks import security


class _FakeRequest:
    def __init__(self, headers):
        self.headers = headers


def test_shared_secret_allows_when_unconfigured():
    # No secret set -> allowed (dev), just logs a warning.
    security.require_shared_secret(_FakeRequest({}), None, "datadog")


def test_shared_secret_rejects_missing_token_when_configured():
    with pytest.raises(HTTPException) as exc:
        security.require_shared_secret(_FakeRequest({}), "s3cret", "datadog")
    assert exc.value.status_code == 401


def test_shared_secret_rejects_wrong_token():
    req = _FakeRequest({"x-webhook-token": "nope"})
    with pytest.raises(HTTPException):
        security.require_shared_secret(req, "s3cret", "datadog")


def test_shared_secret_accepts_correct_token():
    req = _FakeRequest({"x-webhook-token": "s3cret"})
    security.require_shared_secret(req, "s3cret", "datadog")  # no raise


def test_hmac_signature_roundtrip():
    body = b'{"hello":"world"}'
    import hmac, hashlib
    sig = hmac.new(b"key", body, hashlib.sha256).hexdigest()
    assert security.verify_hmac_signature(body, sig, "key") is True
    assert security.verify_hmac_signature(body, "sha256=" + sig, "key") is True
    assert security.verify_hmac_signature(body, "", "key") is False
    assert security.verify_hmac_signature(body, "deadbeef", "key") is False
