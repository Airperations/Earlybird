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
    # Additive fields for structured-log sources (e.g. Datadog Stellar events).
    # All optional with defaults so every existing normalizer call is unaffected.
    event_id: Optional[str] = None          # provider event id (or trace/span fallback)
    title: Optional[str] = None             # human title built at normalize time
    error_message: Optional[str] = None     # nested error.message, kept distinct from `message`
    level: Optional[str] = None             # log level: error | critical | warning | …
    attempts: Optional[int] = None          # retry/attempt count, when the source reports it
    # Non-PII structured metadata preserved for the audit trail. NEVER contains a
    # raw stack trace, user id, email, or secret (see _structured_log_metadata).
    metadata: dict = field(default_factory=dict)


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

    A second, structured-log shape (Datadog log pipelines forwarding app JSON,
    e.g. the Stellar transaction handlers) carries a ``dd`` reserved-attributes
    block and a nested ``error`` object instead of monitor fields. It is detected
    and mapped separately so the monitor path above is never disturbed.
    """
    if _is_structured_log_event(payload):
        return _normalize_datadog_structured_log(payload)

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

    # Business action: an explicit field wins; otherwise recognise a Stellar
    # transaction monitor (e.g. "Stellar Message Store Lag on
    # stellar-cosmoem-buildtransaction - BuildTransaction") from its title/tags so
    # it isn't mis-derived as withdrawal/payment; finally fall back to the
    # endpoint-based derivation. A non-Stellar monitor sees no behaviour change.
    business_action = payload.get("business_action") or tags.get("business_action")
    stellar_action = None
    if not business_action:
        stellar_action = _stellar_action_from_monitor(payload, tags, message)
        business_action = stellar_action or derive_business_action(endpoint, http_status, exception_type)

    provider = (
        ("stellar" if stellar_action else None)
        or tags.get("provider")
        or payload.get("provider")
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
        provider=provider,
        payment_method=tags.get("payment_method") or payload.get("payment_method"),
        business_action=business_action,
    )


# ─── Datadog Stellar / structured-log support ─────────────────────────────────

# Signals that mark a payload as a Stellar transaction event, across both the
# structured-log shape and the metric-monitor shape (title/tags). Matched as
# lower-cased substrings, so "stellar-cosmoem-buildtransaction" and the camelCase
# type "BuildTransaction" both hit.
_STELLAR_SIGNALS = (
    "stellar",
    "buildtransaction",
    "submittransaction",
    "message store lag",
)


def _is_structured_log_event(payload: dict) -> bool:
    """
    True for the app-JSON structured-log shape Datadog forwards from log pipelines
    (a ``dd`` reserved-attributes block and/or a nested ``error`` object, or the
    message-store stream fields). The monitor/custom-webhook shape has none of
    these, so the existing monitor path is never rerouted.
    """
    if not isinstance(payload, dict):
        return False
    if isinstance(payload.get("dd"), dict):
        return True
    if isinstance(payload.get("error"), dict):
        return True
    return any(payload.get(k) for k in ("streamName", "consumerGroupId"))


def _lookup(payload: dict, tags: dict, *keys):
    """First present, non-empty value across payload then tags, for any key form
    (camelCase ``consumerGroupId``, snake ``consumer_group_id``, dotted tag
    ``consumer_group_id.name``)."""
    for src in (payload, tags):
        if not isinstance(src, dict):
            continue
        for k in keys:
            v = src.get(k)
            if v not in (None, ""):
                return v
    return None


def _looks_stellar(blob: str) -> bool:
    b = (blob or "").lower()
    return any(sig in b for sig in _STELLAR_SIGNALS)


def _classify_stellar_action(blob: str) -> str:
    """
    Pick the Stellar business action from the combined signal text. Lag/retry
    storms win over build/submit (a lag monitor is fundamentally about delay).
    All of these resolve to base_action "stellar" (see app.taxonomy).
    """
    b = (blob or "").lower()
    if ("lag" in b or "retried too many times" in b or "too many times" in b
            or "delayed" in b or "delay" in b or "stuck" in b):
        return "stellar_lag"
    if "submit" in b:
        return "stellar_transaction_submit"
    if "build" in b:
        return "stellar_transaction_build"
    return "stellar_transaction"


def _stellar_action_from_monitor(payload: dict, tags: dict, message: str):
    """Detect a Stellar transaction monitor from its title / tags / dimensions.
    Returns a stellar_* business action, or None when no Stellar signal is found."""
    title = payload.get("title") or payload.get("alert_title") or ""
    consumer = _lookup(payload, tags, "consumerGroupId", "consumer_group_id",
                       "consumer_group_id.name", "consumerGroup", "consumer_group") or ""
    category = _lookup(payload, tags, "category", "category.name", "categoryName") or ""
    type_ = payload.get("type") or _lookup(payload, tags, "type", "type.name") or ""
    stream = payload.get("streamName") or payload.get("namespace") or ""
    tag_values = " ".join(str(v) for v in tags.values()) if isinstance(tags, dict) else ""
    blob = " ".join(str(x) for x in [title, message, consumer, category, type_, stream, tag_values] if x)
    if not _looks_stellar(blob):
        return None
    return _classify_stellar_action(blob)


def _build_stellar_title(message: str, type_: str, category: str, service: str) -> str:
    """Human title, e.g. 'Stellar BuildTransaction — message has been retried too many times'."""
    label = type_ or category or service or "transaction"
    msg = (message or "").strip()
    head = f"Stellar {label}".strip()
    return f"{head} — {msg}" if msg else head


def _structured_log_metadata(payload: dict, dd: dict) -> dict:
    """
    Preserve useful, NON-PII fields for the audit trail. Deliberately excludes the
    raw stack trace, any user id/email, and secrets — so nothing sensitive can
    reach a dashboard-visible field. None values are dropped.
    """
    candidates = {
        "consumerGroupId": payload.get("consumerGroupId"),
        "consumerGroupMember": payload.get("consumerGroupMember"),
        "consumerGroupSize": payload.get("consumerGroupSize"),
        "category": payload.get("category"),
        "namespace": payload.get("namespace"),
        "streamName": payload.get("streamName"),
        "type": payload.get("type"),
        "attempts": _safe_int(payload.get("attempts")),
        "level": payload.get("level"),
        "dd.env": dd.get("env"),
        "dd.service": dd.get("service"),
        "dd.version": dd.get("version"),
        "dd.trace_id": dd.get("trace_id"),
        "dd.span_id": dd.get("span_id"),
    }
    return {k: v for k, v in candidates.items() if v is not None}


def _normalize_datadog_structured_log(payload: dict) -> NormalizedEventSchema:
    """Map the Datadog Stellar structured-log shape (dd + error + stream fields)."""
    dd = payload.get("dd") if isinstance(payload.get("dd"), dict) else {}
    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    tags = _parse_tags(payload.get("tags"))

    service = payload.get("service") or dd.get("service") or tags.get("service") or "unknown"
    environment = dd.get("env") or payload.get("env") or tags.get("env") or "production"
    event_id = payload.get("id") or dd.get("trace_id") or dd.get("span_id")
    level = str(payload.get("level")).lower() if payload.get("level") else None
    attempts = _safe_int(payload.get("attempts"))

    category = payload.get("category") or _lookup(payload, tags, "category", "category.name") or ""
    namespace = payload.get("namespace") or ""
    type_ = payload.get("type") or ""
    stream = payload.get("streamName") or ""
    consumer = _lookup(payload, tags, "consumerGroupId", "consumer_group_id",
                       "consumer_group_id.name") or ""
    top_message = payload.get("message") or ""
    error_message = error.get("message")
    exception_type = error.get("name")

    # endpoint/operation: namespace preferred (it names the operation), else the
    # category/type. The raw stack trace is never used here.
    endpoint = namespace or category or type_ or ""

    blob = " ".join(str(x) for x in [category, stream, type_, top_message, consumer, error_message] if x)
    if _looks_stellar(blob):
        business_action = _classify_stellar_action(blob)
        provider = "stellar"
    else:
        # Not a Stellar event — degrade to endpoint derivation (may be None).
        business_action = derive_business_action(endpoint, None, exception_type)
        provider = None

    title = _build_stellar_title(top_message, type_, category, service)
    fingerprint = _build_fingerprint(
        service=service, endpoint=endpoint, http_status=None, exception_type=exception_type,
    )

    return NormalizedEventSchema(
        source="datadog",
        service=service,
        environment=environment,
        endpoint=endpoint,
        url="",
        http_status=None,
        exception_type=exception_type,
        message=title or top_message or error_message,
        user_id=None,
        country=tags.get("country") or payload.get("country"),
        platform=tags.get("platform") or payload.get("platform"),
        release=dd.get("version") or payload.get("version"),
        fingerprint=fingerprint,
        raw_payload=payload,
        event_timestamp=_parse_iso(payload.get("timestamp") or payload.get("time")),
        provider=provider,
        payment_method=None,
        business_action=business_action,
        event_id=event_id,
        title=title,
        error_message=error_message,
        level=level,
        attempts=attempts,
        metadata=_structured_log_metadata(payload, dd),
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
