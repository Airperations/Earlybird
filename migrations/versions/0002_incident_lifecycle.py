"""incident lifecycle timestamps + notification delivery tracking

Revision ID: 0002_incident_lifecycle
Revises: 0001_initial_schema
Create Date: 2026-05-29

Adds the fast-path lifecycle columns to `incidents`:
  detected_at / notification_attempted_at / notification_delivered_at / enriched_at,
  notification_status, notification_attempts, slack_thread_ts.

These back the core winning guarantee: agent_alert_timestamp (the benchmark
field) is set ONLY when notification_delivered_at is set — i.e. after Slack
confirms real delivery, never before. LLM enrichment is recorded separately in
enriched_at so it provably happens *after* the first alert.
"""
from alembic import op
import sqlalchemy as sa

revision = "0002_incident_lifecycle"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("incidents", sa.Column("detected_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("incidents", sa.Column("notification_attempted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("incidents", sa.Column("notification_delivered_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("incidents", sa.Column("enriched_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "incidents",
        sa.Column("notification_status", sa.String(20), nullable=False, server_default="pending"),
    )
    op.add_column(
        "incidents",
        sa.Column("notification_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("incidents", sa.Column("slack_thread_ts", sa.String(64), nullable=True))

    # The matcher scans for incidents that have a delivered alert but no match yet.
    op.create_index(
        "ix_incidents_notification_status", "incidents", ["notification_status"]
    )


def downgrade() -> None:
    op.drop_index("ix_incidents_notification_status", table_name="incidents")
    op.drop_column("incidents", "slack_thread_ts")
    op.drop_column("incidents", "notification_attempts")
    op.drop_column("incidents", "notification_status")
    op.drop_column("incidents", "enriched_at")
    op.drop_column("incidents", "notification_delivered_at")
    op.drop_column("incidents", "notification_attempted_at")
    op.drop_column("incidents", "detected_at")
