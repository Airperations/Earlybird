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


def normalize_sentry(payload: dict) -> NormalizedEventSchema:
    """Parse a Sentry issue webhook payload."""
    event = payload.get("event", {})
    request_data = event.get("request", {})
    tags = {t[0]: t[1] for t in event.get("tags", []) if len(t) == 2}

    url = request_data.get("url", "")
    endpoint = _extract_endpoint(url)
    http_status = _safe_int(
        event.get("contexts", {}).get("response", {}).get("status_code")
        or tags.get("http.status_code")
    )
    exception_type = None
    message = event.get("message") or event.get("title", "")

    exceptions = event.get("exception", {}).get("values", [])
    if exceptions:
        exc = exceptions[0]
        exception_type = exc.get("type")
        message = exc.get("value") or message

    fingerprint = _build_fingerprint(
        service=payload.get("project_slug", "unknown"),
        endpoint=endpoint,
        http_status=http_status,
        exception_type=exception_type,
    )

    return NormalizedEventSchema(
        source="sentry",
        service=payload.get("project_slug", "unknown"),
        environment=tags.get("environment", "production"),
        endpoint=endpoint,
        url=url,
        http_status=http_status,
        exception_type=exception_type,
        message=message,
        user_id=event.get("user", {}).get("id"),
        country=tags.get("country_code"),
        platform=event.get("platform"),
        release=event.get("release"),
        fingerprint=fingerprint,
        raw_payload=payload,
        event_timestamp=_parse_iso(event.get("timestamp")),
    )


def normalize_datadog(payload: dict) -> NormalizedEventSchema:
    """Parse a Datadog monitor alert webhook payload."""
    alert_title = payload.get("alert_title", "")
    url = payload.get("url", "")
    endpoint = _extract_endpoint(url)
    tags_list = payload.get("tags", "").split(",") if payload.get("tags") else []
    tags = dict(t.split(":", 1) for t in tags_list if ":" in t)

    fingerprint = _build_fingerprint(
        service=tags.get("service", "unknown"),
        endpoint=endpoint,
        http_status=None,
        exception_type=payload.get("alert_type"),
    )

    return NormalizedEventSchema(
        source="datadog",
        service=tags.get("service", "unknown"),
        environment=tags.get("env", "production"),
        endpoint=endpoint,
        url=url,
        http_status=None,
        exception_type=payload.get("alert_type"),
        message=alert_title,
        user_id=None,
        country=tags.get("country"),
        platform=tags.get("platform"),
        release=tags.get("version"),
        fingerprint=fingerprint,
        raw_payload=payload,
        event_timestamp=_parse_iso(payload.get("date")),
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
