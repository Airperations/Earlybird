"""
Earlybird — Webhook authentication helpers.

The win-rate metric is only trustworthy if no one can inject fake Sentry errors
(manufacture wins) or fake Freshdesk tickets (manufacture losses). These helpers
enforce a shared secret on the simple webhooks and a real HMAC check on Sentry.
"""

import hmac
import hashlib
import logging
from typing import Optional

from fastapi import Request, HTTPException

logger = logging.getLogger(__name__)


def require_shared_secret(request: Request, expected: Optional[str], source: str,
                          header: str = "x-webhook-token") -> None:
    """
    Enforce a shared-secret header when `expected` is configured.

    Fail-closed when a secret is set: a missing or wrong token → 401. When no
    secret is configured we allow the request (so local dev keeps working) but
    log a warning so an unauthenticated production endpoint is visible.
    """
    if not expected:
        logger.warning(f"[{source.upper()}] No webhook secret configured — endpoint is UNAUTHENTICATED")
        return
    provided = request.headers.get(header, "")
    if not (provided and hmac.compare_digest(provided, expected)):
        raise HTTPException(status_code=401, detail=f"Invalid or missing {source} webhook token")


def verify_hmac_signature(payload: bytes, signature: str, secret: str) -> bool:
    """Constant-time HMAC-SHA256 verification. `signature` may carry a 'sha256=' prefix."""
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, (signature or "").replace("sha256=", ""))


async def parse_json_or_400(request: Request) -> dict:
    """Read the JSON body, returning HTTP 400 (not 500) on a malformed/empty body."""
    try:
        return await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Request body must be valid JSON")
