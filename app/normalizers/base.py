"""
Earlybird — Event Normalizer
Converts raw payloads from Sentry, Datadog, and product events
into a unified NormalizedEventSchema.
"""

import hashlib
import re
from urllib.parse import urlparse
from typing import Optional
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.taxonomy import derive_business_action


@dataclass
class NormalizedEventSchema:
    """Unified event format across all sources."""
    source: str
    service: str
    environment: str
    endpoint: str
    url: str
    http_status: Optional[int]
    exception_type: Optional[str]
    message: Optional[str]
    user_id: Optional[str]
    country: Optional[str]
    platform: Optional[str]
    release: Optional[str]
    fingerprint: str
    raw_payload: dict
    event_timestamp: Optional[datetime] = None
    # Structured business metadata (lets an incident describe itself without the LLM).
    provider: Optional[str] = None          # payment / infra provider, e.g. "stripe"
    payment_method: Optional[str] = None    # e.g. "card", "bank_transfer", "crypto"
    business_action: Optional[str] = None   # e.g. "withdrawal_failed" (see app.taxonomy)


def normalize_sentry(payload: dict) -> NormalizedEventSchema:
    """Parse a Sentry issue/error webhook payload.

    Tolerant of the two tag shapes Sentry emits in the wild: the issue-webhook
    list of ``[key, value]`` pairs and the dict form some integrations send.
    """
    event = payload.get("event") if isinstance(payload, dict) else None
    event = event if isinstance(event, dict) else {}
    request_data = event.get("request") if isinstance(event.get("request"), dict) else {}
    tags = _parse_tags(event.get("tags"))

    contexts = event.get("contexts") if isinstance(event.get("contexts"), dict) else {}
    response_ctx = contexts.get("response") if isinstance(contexts.get("response"), dict) else {}

    url = request_data.get("url", "") or ""
    endpoint = _extract_endpoint(url)
    http_status = _safe_int(
        response_ctx.get("status_code")
        or tags.get("http.status_code")
        or tags.get("http_status_code")
        or request_data.get("status_code")
    )
    exception_type = None
    # `title` and `culprit` are useful human context; fall back through them.
    message = event.get("message") or event.get("title") or event.get("culprit", "")

    exception = event.get("exception") if isinstance(event.get("exception"), dict) else {}
    exceptions = exception.get("values") if isinstance(exception.get("values"), list) else []
    if exceptions and isinstance(exceptions[0], dict):
        exc = exceptions[0]
        exception_type = exc.get("type")
        message = exc.get("value") or message

    service = payload.get("project_slug") or payload.get("project") or "unknown"
    # Environment can live at event-level or in tags depending on SDK/integration.
    environment = event.get("environment") or tags.get("environment") or "production"
    # Prefer an explicit user id; fall back to a username/email so distinct
    # affected users are still counted (the value is hashed downstream, never
    # persisted raw — see app.redaction / service.save_normalized_event).
    user = event.get("user") if isinstance(event.get("user"), dict) else {}
    user_id = user.get("id") or user.get("username") or user.get("email")
    geo = user.get("geo") if isinstance(user.get("geo"), dict) else {}
    country = tags.get("country_code") or tags.get("country") or geo.get("country_code")

    fingerprint = _build_fingerprint(
        service=service,
        endpoint=endpoint,
        http_status=http_status,
        exception_type=exception_type,
    )

    return NormalizedEventSchema(
        source="sentry",
        service=service,
        environment=environment,
        endpoint=endpoint,
        url=url,
        http_status=http_status,
        exception_type=exception_type,
        message=message,
        user_id=user_id,
        country=country,
        platform=event.get("platform"),
        release=event.get("release") or tags.get("release"),
        fingerprint=fingerprint,
        raw_payload=payload,
        event_timestamp=_parse_iso(event.get("timestamp") or payload.get("timestamp")),
        provider=tags.get("provider"),
        payment_method=tags.get("payment_method"),
        business_action=derive_business_action(endpoint, http_status, exception_type),
    )


def normalize_datadog(payload: dict) -> NormalizedEventSchema:
    """Parse a Datadog monitor alert / custom webhook payload.

    Datadog tags reach us in three shapes depending on how the webhook is wired:
      • a comma string         "service:payments-api,country:MX,provider:stripe"
      • a list of "k:v" strings ["service:payments-api", "country:MX"]
      • a JSON object          {"service": "payments-api", "country": "MX"}
    A *custom* webhook payload (recommended) can additionally send first-class
    ``url`` / ``endpoint`` / ``business_action`` / ``service`` / ``env`` fields.
    """
    tags = _parse_tags(payload.get("tags"))

    # Title/message fall back across the standard monitor fields and a custom one.
    message = (
        payload.get("title")
        or payload.get("alert_title")
        or payload.get("message")
        or payload.get("event_title")
        or ""
    )
    url = payload.get("url", "") or ""
    endpoint = _extract_endpoint(url) or payload.get("endpoint", "")
    service = payload.get("service") or tags.get("service") or "unknown"
    http_status = _safe_int(payload.get("http_status") or tags.get("http_status_code"))
    # alert_type / alert_status describe the monitor transition (error / warning…).
    exception_type = payload.get("alert_type") or payload.get("alert_status")

    business_action = (
        payload.get("business_action")
        or tags.get("business_action")
        or derive_business_action(endpoint, http_status, exception_type)
    )

    fingerprint = _build_fingerprint(
        service=service,
        endpoint=endpoint,
        http_status=http_status,
        exception_type=exception_type,
    )

    return NormalizedEventSchema(
        source="datadog",
        service=service,
        environment=payload.get("env") or tags.get("env") or "production",
        endpoint=endpoint,
        url=url,
        http_status=http_status,
        exception_type=exception_type,
        message=message,
        user_id=None,
        country=tags.get("country") or payload.get("country"),
        platform=tags.get("platform") or payload.get("platform"),
        release=tags.get("version") or payload.get("version"),
        fingerprint=fingerprint,
        raw_payload=payload,
        event_timestamp=_parse_iso(payload.get("timestamp") or payload.get("date")),
        provider=tags.get("provider") or payload.get("provider"),
        payment_method=tags.get("payment_method") or payload.get("payment_method"),
        business_action=business_action,
    )


def normalize_product_event(payload: dict) -> NormalizedEventSchema:
    """Parse a custom product event from Airdrive platform."""
    endpoint = payload.get("endpoint", payload.get("path", ""))
    fingerprint = _build_fingerprint(
        service=payload.get("service", "unknown"),
        endpoint=endpoint,
        http_status=payload.get("http_status"),
        exception_type=payload.get("error_type"),
    )

    return NormalizedEventSchema(
        source="product",
        service=payload.get("service", "unknown"),
        environment=payload.get("environment", "production"),
        endpoint=endpoint,
        url=payload.get("url", ""),
        http_status=payload.get("http_status"),
        exception_type=payload.get("error_type"),
        message=payload.get("message"),
        user_id=payload.get("user_id"),
        country=payload.get("country"),
        platform=payload.get("platform"),
        release=payload.get("release"),
        fingerprint=fingerprint,
        raw_payload=payload,
        event_timestamp=_parse_iso(payload.get("timestamp")),
        provider=payload.get("provider"),
        payment_method=payload.get("payment_method"),
        business_action=payload.get("business_action")
        or derive_business_action(endpoint, payload.get("http_status"), payload.get("error_type")),
    )


def normalize(source: str, payload: dict) -> NormalizedEventSchema:
    """Route to the correct normalizer based on source."""
    if source == "sentry":
        return normalize_sentry(payload)
    elif source == "datadog":
        return normalize_datadog(payload)
    elif source == "product":
        return normalize_product_event(payload)
    else:
        raise ValueError(f"Unknown source: {source}")


# ─── Helpers ────────────────────────────────────────────────────────────────

def _parse_tags(raw) -> dict:
    """
    Normalize the many tag shapes Sentry/Datadog emit into a flat ``{key: value}``
    dict. Never raises on an unexpected shape — unknown forms yield ``{}`` so a
    partial payload degrades to "no tags" instead of a 500.

    Supported inputs:
      • dict                       {"service": "payments-api", "country": "MX"}
      • comma string               "service:payments-api,country:MX"
      • list of "k:v" strings      ["service:payments-api", "country:MX"]
      • list of [k, v] pairs       [["environment", "production"], ...]  (Sentry)
      • any mix of the above
    """
    out: dict = {}
    if not raw:
        return out

    if isinstance(raw, dict):
        for k, v in raw.items():
            out[str(k)] = "" if v is None else str(v)
        return out

    items = raw.split(",") if isinstance(raw, str) else raw
    if not isinstance(items, (list, tuple)):
        return out

    for item in items:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            out[str(item[0])] = "" if item[1] is None else str(item[1])
        elif isinstance(item, str) and ":" in item:
            k, v = item.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def _extract_endpoint(url: str) -> str:
    """Extract the base endpoint path, stripping scheme/host, query params and IDs."""
    if not url:
        return ""
    # Strip scheme + host so the fingerprint keys on the path, not the domain.
    parsed = urlparse(url)
    path = parsed.path if parsed.scheme else url.split("?")[0]
    if not path:
        path = url.split("?")[0]
    # Normalize UUIDs and numeric IDs to :id
    path = re.sub(r"/[0-9a-f-]{8,}", "/:id", path)
    path = re.sub(r"/\d+", "/:id", path)
    return path


def _build_fingerprint(
    service: str,
    endpoint: str,
    http_status: Optional[int],
    exception_type: Optional[str],
) -> str:
    """Build a stable fingerprint for deduplication."""
    raw = f"{service}:{endpoint}:{http_status}:{exception_type}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _safe_int(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
