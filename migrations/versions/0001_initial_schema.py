"""initial schema (7 tables) + hot-path indexes

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-05-28

Hand-authored to match app/models.py. Includes the affected_user_ids column
(audit fix C4) and indexes on the dedup/matcher hot paths (audit recommendation #2).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "raw_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_payload", postgresql.JSONB(), nullable=False),
        sa.Column("processed", sa.Boolean(), nullable=True),
    )

    op.create_table(
        "normalized_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("raw_event_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("raw_events.id"), nullable=True),
        sa.Column("source", sa.String(50)),
        sa.Column("service", sa.String(100)),
        sa.Column("environment", sa.String(50)),
        sa.Column("endpoint", sa.String(255)),
        sa.Column("url", sa.String(500)),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("exception_type", sa.String(255), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("user_id", sa.String(255), nullable=True),
        sa.Column("country", sa.String(10), nullable=True),
        sa.Column("platform", sa.String(50), nullable=True),
        sa.Column("release", sa.String(100), nullable=True),
        sa.Column("fingerprint", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_normalized_events_fingerprint", "normalized_events", ["fingerprint"])

    op.create_table(
        "incidents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("fingerprint", sa.String(255), nullable=False),
        sa.Column("title", sa.String(500), nullable=True),
        sa.Column("status", sa.String(50), nullable=False, server_default="new"),
        sa.Column("severity", sa.String(20), nullable=True),
        sa.Column("score", sa.Integer(), server_default="0"),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("affected_users_count", sa.Integer(), server_default="0"),
        sa.Column("affected_user_ids", postgresql.JSONB(), nullable=True),
        sa.Column("event_count", sa.Integer(), server_default="1"),
        sa.Column("countries", postgresql.JSONB(), nullable=True),
        sa.Column("agent_alert_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("slack_message_id", sa.String(255), nullable=True),
        sa.Column("llm_summary", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_incidents_fingerprint", "incidents", ["fingerprint"], unique=True)
    op.create_index("ix_incidents_status", "incidents", ["status"])
    op.create_index("ix_incidents_agent_alert_timestamp", "incidents", ["agent_alert_timestamp"])
    op.create_index("ix_incidents_last_seen_at", "incidents", ["last_seen_at"])

    op.create_table(
        "incident_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("incident_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("incidents.id"), nullable=True),
        sa.Column("normalized_event_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("normalized_events.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "freshdesk_tickets",
        sa.Column("id", sa.String(50), primary_key=True),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("requester_email", sa.String(255), nullable=True),
        sa.Column("tags", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_payload", postgresql.JSONB(), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "incident_freshdesk_matches",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("incident_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("incidents.id"), nullable=True),
        sa.Column("freshdesk_ticket_id", sa.String(50), sa.ForeignKey("freshdesk_tickets.id"), nullable=True),
        sa.Column("agent_alert_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("freshdesk_ticket_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("time_delta_seconds", sa.Integer(), nullable=True),
        sa.Column("outcome", sa.String(20), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("evidence", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_incident_freshdesk_matches_ticket",
        "incident_freshdesk_matches",
        ["freshdesk_ticket_id"],
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("incident_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("event", sa.String(255), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("details", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("audit_log")
    op.drop_index("ix_incident_freshdesk_matches_ticket", table_name="incident_freshdesk_matches")
    op.drop_table("incident_freshdesk_matches")
    op.drop_table("freshdesk_tickets")
    op.drop_table("incident_events")
    op.drop_index("ix_incidents_last_seen_at", table_name="incidents")
    op.drop_index("ix_incidents_agent_alert_timestamp", table_name="incidents")
    op.drop_index("ix_incidents_status", table_name="incidents")
    op.drop_index("ix_incidents_fingerprint", table_name="incidents")
    op.drop_table("incidents")
    op.drop_index("ix_normalized_events_fingerprint", table_name="normalized_events")
    op.drop_table("normalized_events")
    op.drop_table("raw_events")
