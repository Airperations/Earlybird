"""
Earlybird — Webhook authentication helpers.

The win-rate metric is only trustworthy if no one can inject fake Sentry errors
(manufacture wins) or fake Freshdesk tickets (manufacture losses). These helpers
enforce a shared secret on the simple webhooks and a real HMAC check on Sentry.
"""

import hmac
import hashlib
import logging
import time
from datetime import datetime
from typing import Optional

import redis.asyncio as aioredis
from fastapi import Request, HTTPException

from app.config import settings

logger = logging.getLogger(__name__)

# Default acceptable clock skew / replay window for webhooks (seconds).
REPLAY_WINDOW_SECONDS = 300

_redis_client = None


def _get_redis():
    """Lazily create a shared async Redis client (used for nonce single-use tracking)."""
    global _redis_client
    if _redis_client is None:
        _redis_client = aioredis.from_url(settings.REDIS_URL)
    return _redis_client


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


def _parse_timestamp(raw: str) -> Optional[float]:
    """Accept either epoch seconds or an ISO-8601 string; return epoch seconds."""
    try:
        return float(raw)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


async def enforce_replay_protection(
    request: Request,
    source: str,
    window_seconds: int = REPLAY_WINDOW_SECONDS,
    redis_client=None,
) -> None:
    """
    Reject stale or replayed webhooks. Complements the auth in require_shared_secret /
    verify_hmac_signature: auth proves the payload is authentic, this proves it is
    *fresh and used once*, so a captured-but-valid request can't be re-sent later.

    Two independent, opt-in checks based on caller-supplied headers:
      • `x-webhook-timestamp` (epoch or ISO): rejected if outside ±window_seconds.
      • `x-webhook-nonce`: rejected if it was already seen within the window
        (tracked in Redis with SET NX EX).

    Headers are optional so providers that can't send them still work, but for the
    endpoints we control the sender on (Datadog/Freshdesk/product webhooks) sending
    both gives full replay protection. Fail-open if Redis is unavailable.
    """
    ts_raw = request.headers.get("x-webhook-timestamp")
    if ts_raw:
        ts = _parse_timestamp(ts_raw)
        if ts is None:
            raise HTTPException(status_code=400, detail="Invalid x-webhook-timestamp")
        if abs(time.time() - ts) > window_seconds:
            raise HTTPException(status_code=401, detail=f"Stale {source} webhook (possible replay)")

    nonce = request.headers.get("x-webhook-nonce")
    if nonce:
        client = redis_client or _get_redis()
        try:
            fresh = await client.set(
                f"earlybird:nonce:{source}:{nonce}", "1", nx=True, ex=window_seconds
            )
        except Exception as e:
            logger.warning(f"[{source.upper()}] Nonce store unavailable ({e}); skipping replay check")
            return
        if not fresh:
            raise HTTPException(status_code=401, detail=f"Replayed {source} webhook nonce")


async def parse_json_or_400(request: Request) -> dict:
    """Read the JSON body, returning HTTP 400 (not 500) on a malformed/empty body."""
    try:
        return await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Request body must be valid JSON")
