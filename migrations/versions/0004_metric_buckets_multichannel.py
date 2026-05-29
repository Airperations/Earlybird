"""metric buckets (rolling baseline) + multichannel delivery channel

Revision ID: 0004_metric_buckets_multichannel
Revises: 0003_metadata_recurrence_pii
Create Date: 2026-05-29

1. metric_buckets — per-minute, per-dimension business metrics the agent builds
   for itself, enabling rolling-baseline anomaly detection (success-rate drop,
   failure/pending rate, volume z-score, latency regression) by country / provider
   / platform / payment_method.

2. incidents.notification_channel — which channel (slack | pagerduty | email)
   actually delivered the benchmark alert, so multichannel fallback is auditable.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004_metric_buckets_multichannel"
down_revision = "0003_metadata_recurrence_pii"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("incidents", sa.Column("notification_channel", sa.String(20), nullable=True))

    op.create_table(
        "metric_buckets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("business_action", sa.String(100), nullable=False),
        sa.Column("dimension", sa.String(30), nullable=False),
        sa.Column("dimension_value", sa.String(100), nullable=False),
        sa.Column("total_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("success_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pending_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("latency_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("latency_sum_ms", sa.Float(), nullable=False, server_default="0"),
        sa.Column("latency_max_ms", sa.Float(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "uq_metric_bucket_cell", "metric_buckets",
        ["bucket_start", "business_action", "dimension", "dimension_value"], unique=True,
    )
    op.create_index(
        "ix_metric_buckets_lookup", "metric_buckets",
        ["business_action", "dimension", "dimension_value", "bucket_start"],
    )


def downgrade() -> None:
    op.drop_index("ix_metric_buckets_lookup", table_name="metric_buckets")
    op.drop_index("uq_metric_bucket_cell", table_name="metric_buckets")
    op.drop_table("metric_buckets")
    op.drop_column("incidents", "notification_channel")
