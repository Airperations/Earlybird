"""
Earlybird — Database Models
Full audit trail schema for bounty evidence.
"""

import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    Column, String, Integer, Float, Boolean, Text,
    DateTime, ForeignKey, JSON
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
    user_id = Column(String(255), nullable=True)
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

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    fingerprint = Column(String(255), nullable=False, unique=True, index=True)
    title = Column(String(500), nullable=True)

    # State machine: new → observing → alerted → matched → resolved | ignored
    status = Column(String(50), nullable=False, default="new")
    severity = Column(String(20), nullable=True)       # critical | high | medium | low
    score = Column(Integer, default=0)

    first_seen_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    last_seen_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    affected_users_count = Column(Integer, default=0)
    affected_user_ids = Column(JSONB, default=list)   # distinct user ids seen for this incident
    event_count = Column(Integer, default=1)
    countries = Column(JSONB, default=list)

    # === BOUNTY KEY FIELD ===
    # This timestamp is compared against Freshdesk ticket created_at
    agent_alert_timestamp = Column(DateTime(timezone=True), nullable=True)

    slack_message_id = Column(String(255), nullable=True)
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
    requester_email = Column(String(255), nullable=True)
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
