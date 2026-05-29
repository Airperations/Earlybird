"""
Earlybird — Database Models
Full audit trail schema for bounty evidence.
"""

import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    Column, String, Integer, Float, Boolean, Text,
    DateTime, ForeignKey, JSON, Index, text
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship
from app.database import Base


def utcnow():
    return datetime.now(timezone.utc)


class RawEvent(Base):
    """Stores every incoming webhook exactly as received."""
    __tablename__ = "raw_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source = Column(String(50), nullable=False)           # sentry | datadog | product
    received_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    event_timestamp = Column(DateTime(timezone=True), nullable=True)
    raw_payload = Column(JSONB, nullable=False)
    processed = Column(Boolean, default=False)
    # Deterministic key over (source, received_at, payload). Lets a redelivered
    # Celery task (acks_late at-least-once) recognize a duplicate instead of
    # inserting a second raw event and double-counting the incident.
    idempotency_key = Column(String(64), nullable=True, unique=True, index=True)

    normalized_events = relationship("NormalizedEvent", back_populates="raw_event")


class NormalizedEvent(Base):
    """Cleaned, structured version of a raw event."""
    __tablename__ = "normalized_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    raw_event_id = Column(UUID(as_uuid=True), ForeignKey("raw_events.id"))
    source = Column(String(50))
    service = Column(String(100))
    environment = Column(String(50), default="production")
    endpoint = Column(String(255))
    url = Column(String(500))
    http_status = Column(Integer, nullable=True)
    exception_type = Column(String(255), nullable=True)
    message = Column(Text, nullable=True)
    user_id = Column(String(255), nullable=True)   # SALTED HASH of the user id, never raw (see app.redaction)
    country = Column(String(10), nullable=True)
    platform = Column(String(50), nullable=True)
    release = Column(String(100), nullable=True)
    fingerprint = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    raw_event = relationship("RawEvent", back_populates="normalized_events")
    incident_events = relationship("IncidentEvent", back_populates="normalized_event")


class Incident(Base):
    """
    Grouped incident. The core entity of the system.
    agent_alert_timestamp is the KEY field for the bounty race.
    """
    __tablename__ = "incidents"

    # Fingerprint is NOT globally unique: a resolved/false_positive incident that
    # recurs becomes a NEW row (its own benchmark race). A partial unique index
    # (see __table_args__) instead allows only ONE *open* incident per fingerprint,
    # so live duplicates can't form while history stays clean per recurrence.
    __table_args__ = (
        Index(
            "uq_open_incident_fingerprint",
            "fingerprint",
            unique=True,
            postgresql_where=text("status NOT IN ('resolved', 'false_positive')"),
            sqlite_where=text("status NOT IN ('resolved', 'false_positive')"),
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Non-unique index (lookups by fingerprint); open-uniqueness handled above.
    fingerprint = Column(String(255), nullable=False, index=True)
    title = Column(String(500), nullable=True)

    # ── Structured business metadata ────────────────────────────────────────
    # Populated from the normalized event so the incident can answer "what / where
    # / who" WITHOUT reading the LLM summary, and so the matcher can use these as
    # first-class signals.
    service = Column(String(100), nullable=True)
    endpoint = Column(String(255), nullable=True)
    route = Column(String(255), nullable=True)              # normalized request path
    business_action = Column(String(100), nullable=True, index=True)  # e.g. withdrawal_failed
    http_status = Column(Integer, nullable=True)
    exception_type = Column(String(255), nullable=True)
    primary_country = Column(String(10), nullable=True)
    provider = Column(String(100), nullable=True)           # e.g. stripe
    platform = Column(String(50), nullable=True)            # e.g. ios / android / web
    payment_method = Column(String(50), nullable=True)      # e.g. card / crypto
    normalized_keywords = Column(JSONB, default=list)       # searchable vocabulary footprint

    # State machine:
    #   new → observing → detected → alerted → enriched → matched_to_freshdesk → resolved
    #   (with notification_failed / ignored / false_positive branches)
    status = Column(String(50), nullable=False, default="new")
    severity = Column(String(20), nullable=True)       # critical | high | medium | low
    score = Column(Integer, default=0)

    first_seen_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    last_seen_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    affected_users_count = Column(Integer, default=0)
    # SALTED HASHES of distinct user ids — never raw ids. Keeps the affected-users
    # count accurate and overlap detectable while storing zero PII. See app.redaction.
    affected_user_hashes = Column(JSONB, default=list)
    event_count = Column(Integer, default=1)
    countries = Column(JSONB, default=list)

    # === BOUNTY KEY FIELD ===
    # The OFFICIAL benchmark timestamp compared against Freshdesk ticket created_at.
    # CRITICAL INVARIANT: this is set ONLY after Slack confirms real delivery
    # (== notification_delivered_at). A failed/never-attempted notification leaves
    # it NULL, so the agent can never claim a win it did not actually deliver.
    agent_alert_timestamp = Column(DateTime(timezone=True), nullable=True)

    # ── Lifecycle audit timestamps (the fast-path proof) ───────────────────
    # detected:   incident crossed the alert threshold (alert decision made)
    # attempted:  first Slack delivery attempt began
    # delivered:  Slack confirmed delivery → mirrors agent_alert_timestamp
    # enriched:   LLM summary produced and posted as a follow-up (after delivery)
    detected_at = Column(DateTime(timezone=True), nullable=True)
    notification_attempted_at = Column(DateTime(timezone=True), nullable=True)
    notification_delivered_at = Column(DateTime(timezone=True), nullable=True)
    enriched_at = Column(DateTime(timezone=True), nullable=True)
    notification_status = Column(String(20), nullable=False, default="pending")  # pending|delivered|failed
    notification_attempts = Column(Integer, default=0)

    slack_message_id = Column(String(255), nullable=True)
    slack_thread_ts = Column(String(64), nullable=True)   # parent ts for enrichment thread reply
    # Which channel actually delivered the benchmark alert: slack | pagerduty | email.
    notification_channel = Column(String(20), nullable=True)
    llm_summary = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    events = relationship("IncidentEvent", back_populates="incident")
    freshdesk_matches = relationship("IncidentFreshdeskMatch", back_populates="incident")


class IncidentEvent(Base):
    """Join table: links normalized events to incidents."""
    __tablename__ = "incident_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    incident_id = Column(UUID(as_uuid=True), ForeignKey("incidents.id"))
    normalized_event_id = Column(UUID(as_uuid=True), ForeignKey("normalized_events.id"))
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    incident = relationship("Incident", back_populates="events")
    normalized_event = relationship("NormalizedEvent", back_populates="incident_events")


class FreshdeskTicket(Base):
    """Freshdesk support tickets synced for comparison."""
    __tablename__ = "freshdesk_tickets"

    id = Column(String(50), primary_key=True)           # Freshdesk ticket ID
    subject = Column(Text, nullable=True)
    description = Column(Text, nullable=True)
    requester_email = Column(String(255), nullable=True)  # stores a SALTED HASH, never the raw email
    tags = Column(JSONB, default=list)
    created_at = Column(DateTime(timezone=True), nullable=False)
    raw_payload = Column(JSONB, nullable=True)
    synced_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    matches = relationship("IncidentFreshdeskMatch", back_populates="ticket")


class IncidentFreshdeskMatch(Base):
    """
    The RACE RESULT table.
    Compares agent_alert_timestamp vs freshdesk ticket created_at.
    This is what proves the bounty win rate.
    """
    __tablename__ = "incident_freshdesk_matches"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    incident_id = Column(UUID(as_uuid=True), ForeignKey("incidents.id"))
    freshdesk_ticket_id = Column(String(50), ForeignKey("freshdesk_tickets.id"))

    agent_alert_timestamp = Column(DateTime(timezone=True), nullable=False)
    freshdesk_ticket_timestamp = Column(DateTime(timezone=True), nullable=False)

    # Positive = agent won, negative = agent lost (in seconds)
    time_delta_seconds = Column(Integer, nullable=True)

    # agent_won | agent_lost | tie
    outcome = Column(String(20), nullable=False)
    confidence = Column(Float, nullable=True)
    # Structured, auditable explanation of WHY this ticket matched this incident.
    #   matched_by:    ["business_action", "country", "provider", "time_window", "keyword_match"]
    #   match_reasons: {"business_action": "withdrawal_failed", "country": "MX",
    #                   "keyword_overlap": ["retiro","falló"], "keyword_language": "es",
    #                   "time_delta_seconds": 240, ...}
    matched_by = Column(JSONB, default=list)
    match_reasons = Column(JSONB, default=dict)
    evidence = Column(JSONB, nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    incident = relationship("Incident", back_populates="freshdesk_matches")
    ticket = relationship("FreshdeskTicket", back_populates="matches")


class AuditLog(Base):
    """Immutable audit trail of all agent actions."""
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    incident_id = Column(UUID(as_uuid=True), nullable=True)
    event = Column(String(255), nullable=False)
    timestamp = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    details = Column(JSONB, nullable=True)


class MetricBucket(Base):
    """
    Per-minute, per-dimension business metrics the agent builds for ITSELF from
    the event stream — the substrate for rolling-baseline anomaly detection.

    One row = one (minute, business_action, dimension, dimension_value) cell, e.g.
    (12:03, "withdrawal", "country", "MX"). The agent compares a recent window to
    the preceding baseline window per dimension, so it can catch
    "withdrawal_success_rate in MX dropped 97%→71%" with no producer-side metrics.
    """
    __tablename__ = "metric_buckets"
    __table_args__ = (
        Index(
            "uq_metric_bucket_cell",
            "bucket_start", "business_action", "dimension", "dimension_value",
            unique=True,
        ),
        Index("ix_metric_buckets_lookup", "business_action", "dimension", "dimension_value", "bucket_start"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bucket_start = Column(DateTime(timezone=True), nullable=False)   # truncated to the minute
    business_action = Column(String(100), nullable=False)           # base action, e.g. "withdrawal"
    dimension = Column(String(30), nullable=False)                  # global|country|provider|platform|payment_method
    dimension_value = Column(String(100), nullable=False)           # ALL|MX|stripe|ios|card|...

    total_count = Column(Integer, nullable=False, default=0)
    success_count = Column(Integer, nullable=False, default=0)
    failure_count = Column(Integer, nullable=False, default=0)
    pending_count = Column(Integer, nullable=False, default=0)
    latency_count = Column(Integer, nullable=False, default=0)      # events that carried a latency sample
    latency_sum_ms = Column(Float, nullable=False, default=0.0)
    latency_max_ms = Column(Float, nullable=False, default=0.0)

    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)


class DeadLetterEvent(Base):
    """
    Events that failed every Celery retry land here instead of vanishing.
    A human (or a replay job) can inspect and re-drive them — this is the
    durable dead-letter queue backing the 'no event is dropped' guarantee.
    """
    __tablename__ = "dead_letter_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source = Column(String(50), nullable=False)
    raw_payload = Column(JSONB, nullable=False)
    received_at = Column(DateTime(timezone=True), nullable=True)
    error = Column(Text, nullable=True)
    failed_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    replayed = Column(Boolean, default=False)
