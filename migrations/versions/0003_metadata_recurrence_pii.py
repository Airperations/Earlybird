"""structured incident metadata + recurrence partial index + PII hashing + match reasons

Revision ID: 0003_metadata_recurrence_pii
Revises: 0002_incident_lifecycle
Create Date: 2026-05-29

This migration makes the agent auditable, recurrence-correct, and PII-safe:

1. Structured incident metadata (service/endpoint/route/business_action/http_status/
   exception_type/primary_country/provider/platform/payment_method/normalized_keywords)
   so an incident answers "what / where / who" without the LLM summary.

2. Recurrence redesign — drop the GLOBAL unique constraint on incidents.fingerprint,
   keep a plain non-unique index, and add a PARTIAL unique index that allows only
   ONE *open* incident per fingerprint. A resolved/false_positive fingerprint that
   recurs becomes a NEW incident row = its own benchmark race against Freshdesk.

       CREATE UNIQUE INDEX uq_open_incident_fingerprint
       ON incidents(fingerprint)
       WHERE status NOT IN ('resolved', 'false_positive');

3. PII safety — rename affected_user_ids → affected_user_hashes (now stores salted
   hashes, never raw ids).

4. Match transparency — add matched_by / match_reasons to incident_freshdesk_matches.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003_metadata_recurrence_pii"
down_revision = "0002_incident_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. Structured incident metadata ─────────────────────────────────────
    op.add_column("incidents", sa.Column("service", sa.String(100), nullable=True))
    op.add_column("incidents", sa.Column("endpoint", sa.String(255), nullable=True))
    op.add_column("incidents", sa.Column("route", sa.String(255), nullable=True))
    op.add_column("incidents", sa.Column("business_action", sa.String(100), nullable=True))
    op.add_column("incidents", sa.Column("http_status", sa.Integer(), nullable=True))
    op.add_column("incidents", sa.Column("exception_type", sa.String(255), nullable=True))
    op.add_column("incidents", sa.Column("primary_country", sa.String(10), nullable=True))
    op.add_column("incidents", sa.Column("provider", sa.String(100), nullable=True))
    op.add_column("incidents", sa.Column("platform", sa.String(50), nullable=True))
    op.add_column("incidents", sa.Column("payment_method", sa.String(50), nullable=True))
    op.add_column("incidents", sa.Column("normalized_keywords", postgresql.JSONB(), nullable=True))
    op.create_index("ix_incidents_business_action", "incidents", ["business_action"])

    # ── 2. Recurrence: drop global unique, keep plain index, add partial unique ─
    op.drop_index("ix_incidents_fingerprint", table_name="incidents")
    op.create_index("ix_incidents_fingerprint", "incidents", ["fingerprint"], unique=False)
    op.create_index(
        "uq_open_incident_fingerprint",
        "incidents",
        ["fingerprint"],
        unique=True,
        postgresql_where=sa.text("status NOT IN ('resolved', 'false_positive')"),
    )

    # ── 3. PII: affected_user_ids → affected_user_hashes ────────────────────
    op.alter_column("incidents", "affected_user_ids", new_column_name="affected_user_hashes")

    # ── 4. Match transparency ───────────────────────────────────────────────
    op.add_column("incident_freshdesk_matches", sa.Column("matched_by", postgresql.JSONB(), nullable=True))
    op.add_column("incident_freshdesk_matches", sa.Column("match_reasons", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("incident_freshdesk_matches", "match_reasons")
    op.drop_column("incident_freshdesk_matches", "matched_by")

    op.alter_column("incidents", "affected_user_hashes", new_column_name="affected_user_ids")

    op.drop_index("uq_open_incident_fingerprint", table_name="incidents")
    op.drop_index("ix_incidents_fingerprint", table_name="incidents")
    op.create_index("ix_incidents_fingerprint", "incidents", ["fingerprint"], unique=True)

    op.drop_index("ix_incidents_business_action", table_name="incidents")
    op.drop_column("incidents", "normalized_keywords")
    op.drop_column("incidents", "payment_method")
    op.drop_column("incidents", "platform")
    op.drop_column("incidents", "provider")
    op.drop_column("incidents", "primary_country")
    op.drop_column("incidents", "exception_type")
    op.drop_column("incidents", "http_status")
    op.drop_column("incidents", "business_action")
    op.drop_column("incidents", "route")
    op.drop_column("incidents", "endpoint")
    op.drop_column("incidents", "service")
