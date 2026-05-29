"""
Schema / migration guards.

Alembic migration 0001 uses Postgres-only features (plpgsql triggers), so the
full migration chain can't execute on SQLite. Instead we verify the migration
module is well-formed and that the live ORM schema reflects the 0003 changes —
the partial unique index and the PII column rename in particular.
"""

import importlib

from sqlalchemy import inspect as sa_inspect

from app.models import Incident, IncidentFreshdeskMatch, MetricBucket


def test_migration_0003_is_well_formed():
    mod = importlib.import_module("migrations.versions.0003_metadata_recurrence_pii")
    assert mod.revision == "0003_metadata_recurrence_pii"
    assert mod.down_revision == "0002_incident_lifecycle"
    assert callable(mod.upgrade) and callable(mod.downgrade)


def test_migration_0004_is_well_formed():
    mod = importlib.import_module("migrations.versions.0004_metric_buckets_multichannel")
    assert mod.revision == "0004_metric_buckets_multichannel"
    assert mod.down_revision == "0003_metadata_recurrence_pii"
    assert callable(mod.upgrade) and callable(mod.downgrade)


def test_metric_bucket_table_and_channel_column():
    assert MetricBucket.__tablename__ == "metric_buckets"
    bcols = {c.name for c in MetricBucket.__table__.columns}
    for c in ["bucket_start", "business_action", "dimension", "dimension_value",
              "total_count", "success_count", "failure_count", "pending_count",
              "latency_count", "latency_sum_ms", "latency_max_ms"]:
        assert c in bcols, f"missing metric_bucket column {c}"
    bidx = {i.name for i in MetricBucket.__table__.indexes}
    assert "uq_metric_bucket_cell" in bidx
    assert "notification_channel" in {c.name for c in Incident.__table__.columns}


def test_partial_unique_index_present_in_model():
    idx = {i.name: i for i in Incident.__table__.indexes}
    assert "uq_open_incident_fingerprint" in idx
    partial = idx["uq_open_incident_fingerprint"]
    assert partial.unique is True
    # The non-unique lookup index on fingerprint also exists.
    assert "ix_incidents_fingerprint" in idx
    assert idx["ix_incidents_fingerprint"].unique is False


def test_pii_column_renamed():
    cols = {c.name for c in Incident.__table__.columns}
    assert "affected_user_hashes" in cols
    assert "affected_user_ids" not in cols


def test_structured_metadata_columns_present():
    cols = {c.name for c in Incident.__table__.columns}
    for c in ["service", "endpoint", "route", "business_action", "http_status",
              "exception_type", "primary_country", "provider", "platform",
              "payment_method", "normalized_keywords"]:
        assert c in cols, f"missing incident column {c}"


def test_match_explanation_columns_present():
    cols = {c.name for c in IncidentFreshdeskMatch.__table__.columns}
    assert "matched_by" in cols
    assert "match_reasons" in cols
