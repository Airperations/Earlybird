"""
Datadog Stellar transaction payload support — additive normalization tests.

Two real shapes are covered:
  • the structured-log shape Datadog forwards from app log pipelines (a ``dd``
    reserved-attributes block + a nested ``error`` object + message-store stream
    fields), and
  • the metric-monitor shape ("Stellar Message Store Lag on
    {consumer_group_id.name} - {category.name}") whose Stellar identity lives in
    the title / Datadog tag dimensions.

These prove the payloads normalize as a Stellar business incident (not
withdrawal/payment), score high enough to alert, preserve non-PII metadata
(never the raw stack trace), and match a Stellar Freshdesk ticket — without
disturbing the existing Datadog monitor path.
"""

import uuid
from datetime import datetime, timezone, timedelta

import pytest

from app.normalizers.base import normalize, normalize_datadog
from app.taxonomy import base_action, detect_keyword_overlap
from app.incidents import service
from app.freshdesk import matcher
from app.incidents.scoring import calculate_criticality
from app.config import settings


# ── Real payloads (from the task) ─────────────────────────────────────────────

def _stellar_error_payload() -> dict:
    return {
        "attempts": 668,
        "category": "stellarTransaction:command",
        "consumerGroupId": "stellar-cosmoem-buildTransaction",
        "consumerGroupMember": 2,
        "consumerGroupSize": 3,
        "dd": {
            "env": "kanto",
            "service": "stellar-cosmoem-build-events-handler",
            "span_id": "7588635184472827306",
            "trace_id": "8938160530720518636",
            "version": "1.0.39",
        },
        "error": {
            "message": "invalid stellar public/secret key passed",
            "name": "Error",
            "stack": "Error: invalid stellar public/secret key passed\n    at Object.<anonymous> (/app/ports/stellar-port.js:671:23)",
        },
        "globalPosition": 1246524549,
        "id": "c83361fe-050e-48c4-9c98-b1fcdeabf349",
        "level": "error",
        "message": "error processing message",
        "namespace": "subscriber:processMessage",
        "position": 2,
        "service": "stellar-cosmoem-build-events-handler",
        "sleepFor": 10000,
        "streamName": "stellarTransaction:command-cdade899-2fdc-4bab-a8e9-aba52aa4a0ef",
        "time": "2025-07-29T21:12:02.927Z",
        "timestamp": "2025-07-29T23:05:59.619Z",
        "type": "BuildTransaction",
    }


def _stellar_critical_payload() -> dict:
    p = _stellar_error_payload()
    p.pop("sleepFor", None)
    p["level"] = "critical"
    p["message"] = "message has been retried too many times"
    return p


# ── 1. Normalizes to the new Stellar business action ──────────────────────────

def test_stellar_error_payload_normalizes_to_stellar_action():
    n = normalize("datadog", _stellar_error_payload())
    assert n.source == "datadog"
    assert n.business_action == "stellar_transaction_build"
    assert base_action(n.business_action) == "stellar"
    # Not forced into a withdrawal/payment flow.
    assert "withdraw" not in (n.business_action or "")
    assert "payment" not in (n.business_action or "")
    assert n.provider == "stellar"
    # event_id from `id`; timestamp from `timestamp`.
    assert n.event_id == "c83361fe-050e-48c4-9c98-b1fcdeabf349"
    assert n.event_timestamp == datetime(2025, 7, 29, 23, 5, 59, 619000, tzinfo=timezone.utc)
    assert n.service == "stellar-cosmoem-build-events-handler"
    assert n.environment == "kanto"
    assert n.error_message == "invalid stellar public/secret key passed"
    assert n.exception_type == "Error"
    assert n.title.startswith("Stellar BuildTransaction")


def test_stellar_event_id_and_timestamp_fallbacks():
    """event_id falls back to dd.trace_id/span_id; timestamp falls back to `time`."""
    p = _stellar_error_payload()
    p.pop("id")
    p.pop("timestamp")
    n = normalize("datadog", p)
    assert n.event_id == "8938160530720518636"  # dd.trace_id
    assert n.event_timestamp == datetime(2025, 7, 29, 21, 12, 2, 927000, tzinfo=timezone.utc)


# ── 2. Critical payload → critical/high severity + metadata preserved ─────────

def test_stellar_critical_payload_severity_and_metadata():
    n = normalize("datadog", _stellar_critical_payload())
    assert n.level == "critical"
    assert n.business_action == "stellar_lag"
    assert base_action(n.business_action) == "stellar"

    sc = calculate_criticality(event=n, affected_users=0, event_count=1,
                               countries=[], has_existing_tickets=False)
    assert sc.severity in ("critical", "high")

    # Useful non-PII metadata is preserved.
    md = n.metadata
    assert md["dd.service"] == "stellar-cosmoem-build-events-handler"
    assert md["dd.env"] == "kanto"
    assert md["category"] == "stellarTransaction:command"
    assert md["consumerGroupId"] == "stellar-cosmoem-buildTransaction"
    assert md["consumerGroupMember"] == 2
    assert md["consumerGroupSize"] == 3
    assert md["streamName"].startswith("stellarTransaction:command-")
    assert md["type"] == "BuildTransaction"
    assert md["attempts"] == 668
    assert md["dd.trace_id"] and md["dd.span_id"] and md["dd.version"]


def test_stellar_metadata_never_contains_stack_trace():
    """The raw stack trace must not reach any normalized/metadata field."""
    n = normalize("datadog", _stellar_critical_payload())
    blob = " ".join(str(v) for v in n.metadata.values())
    assert "stack" not in [k.lower() for k in n.metadata.keys()]
    assert "stellar-port.js" not in blob
    assert "stellar-port.js" not in (n.title or "")
    assert "stellar-port.js" not in (n.message or "")


# ── 3. Critical payload crosses the alert threshold (without touching others) ─

def test_stellar_critical_crosses_alert_threshold():
    n = normalize("datadog", _stellar_critical_payload())
    sc = calculate_criticality(event=n, affected_users=0, event_count=1,
                               countries=[], has_existing_tickets=False)
    assert sc.is_critical_path is True
    # Crosses both the critical-business-action bar and the default incident bar.
    assert sc.total_score >= settings.CRITICAL_BUSINESS_ACTION_THRESHOLD
    assert sc.total_score >= settings.INCIDENT_ALERT_THRESHOLD
    assert sc.breakdown["log_level"] > 0
    assert sc.breakdown["retry_storm"] > 0


def test_existing_withdrawal_scoring_unchanged_by_stellar_signals():
    """Regression guard: the canonical /withdraw demo event still scores 125."""
    from app.normalizers.base import NormalizedEventSchema
    wd = NormalizedEventSchema(
        source="sentry", service="payments-api", environment="production",
        endpoint="/withdraw/confirm", url="https://api/x", http_status=502,
        exception_type="GatewayTimeout", message="boom", user_id="u1",
        country="MX", platform="python", release="v1", fingerprint="abc", raw_payload={},
    )
    sc = calculate_criticality(event=wd, affected_users=1, event_count=1,
                               countries=["MX"], has_existing_tickets=False)
    assert sc.total_score == 125
    assert sc.severity == "critical"
    assert sc.breakdown["critical_path"] == 50
    assert sc.breakdown["log_level"] == 0
    assert sc.breakdown["retry_storm"] == 0


# ── 4. base_action handles the new multi-word Stellar action ──────────────────

def test_base_action_handles_multiword_stellar_action():
    assert base_action("stellar_transaction_build") == "stellar"
    assert base_action("stellar_transaction_submit") == "stellar"
    assert base_action("stellar_lag") == "stellar"
    assert base_action("stellar") == "stellar"


# ── 5. Freshdesk matcher links a Stellar ticket to the Stellar incident ───────

@pytest.mark.asyncio
async def test_stellar_incident_matches_freshdesk_ticket(db):
    n = normalize("datadog", _stellar_critical_payload())
    inc = await service.find_or_create_incident(db, n)
    inc.agent_alert_timestamp = datetime(2025, 7, 29, 23, 6, 0, tzinfo=timezone.utc)
    await db.flush()

    # English Stellar ticket within the time window.
    ticket_en = {
        "id": "stellar-en-1",
        "subject": "My stellar transaction is stuck",
        "description": "build transaction failed and my withdrawal via stellar is delayed",
        "tags": [],
        "created_at": "2025-07-29T23:09:00Z",  # +180s → agent won
    }
    exp_en = matcher.explain_match(inc, ticket_en)
    assert exp_en.confidence >= 0.5
    assert "business_action" in exp_en.matched_by
    assert "provider" in exp_en.matched_by  # "stellar" named in the ticket

    # Spanish Stellar ticket.
    ticket_es = {
        "id": "stellar-es-1",
        "subject": "transacción stellar retrasada",
        "description": "mi transacción stellar está demorada y no se completa",
        "tags": [],
        "created_at": "2025-07-29T23:08:00Z",
    }
    exp_es = matcher.explain_match(inc, ticket_es)
    assert exp_es.confidence >= 0.5
    assert exp_es.match_reasons.get("keyword_language") in ("es", "mixed")


def test_stellar_keywords_recognised_by_overlap():
    r = detect_keyword_overlap("build transaction failed, transacción stellar retrasada", "stellar_lag")
    assert r["action_match"] is True
    assert "stellar" in r["groups"]
    assert r["language"] in ("en", "es", "mixed")


# ── 6. Metric-monitor shape is classified as Stellar (not withdrawal/payment) ─

@pytest.mark.parametrize("title,tags,expected", [
    ("[Triggered] Stellar Message Store Lag on stellar-cosmoem-buildtransaction - BuildTransaction",
     ["consumer_group_id.name:stellar-cosmoem-buildtransaction", "category.name:BuildTransaction"],
     "stellar_lag"),
    ("[Triggered] Stellar Message Store Lag on stellar-cosmoem-submittransaction - SubmitTransaction",
     ["consumer_group_id.name:stellar-cosmoem-submittransaction", "category.name:SubmitTransaction"],
     "stellar_lag"),
])
def test_stellar_metric_monitor_classified_as_stellar(title, tags, expected):
    n = normalize("datadog", {"alert_type": "error", "alert_status": "Triggered",
                              "title": title, "message": "lag high", "tags": tags})
    assert n.business_action == expected
    assert base_action(n.business_action) == "stellar"
    assert n.provider == "stellar"


def test_stellar_camelcase_monitor_fields_recognised():
    n = normalize("datadog", {
        "title": "BuildTransaction issue",
        "consumerGroupId": "stellar-cosmoem-buildtransaction",
        "category": "BuildTransaction",
    })
    assert base_action(n.business_action) == "stellar"
    assert n.provider == "stellar"


def _stellar_lag_monitor_payload() -> dict:
    """The real Datadog 'Stellar Message Store Lag' monitor shape (redacted)."""
    return {
        "env": "production",
        "url": "https://app.datadoghq.com/event/event?id=123456",
        "tags": "category:stellartransaction:command,consumer_group_id:stellar-cosmoem-buildtransaction,monitor",
        "title": "[Warn on {consumer_group_id:stellar-cosmoem-buildtransaction}] Stellar Message Store Lag on stellar-cosmoem-buildtransaction - stellartransaction:command",
        "source": "datadog",
        "message": "... **airtm.message_store.lag** over **category:stellartransaction:command,consumer_group_id:stellar-cosmoem-buildtransaction** was **> 200.0** on average during the **last 15m** ...",
        "event_id": "123456",
        "timestamp": "1700000000",
        "monitor_id": "987654",
        "alert_status": "airtm.message_store.lag over category:stellartransaction:command was > 200.0 on average during the last 15m.",
        "monitor_name": "Stellar Message Store Lag on stellar-cosmoem-buildtransaction - stellartransaction:command",
    }


def test_stellar_lag_monitor_display_metadata():
    """A real Stellar Message Store Lag monitor gets friendly service/endpoint and
    keeps the event URL as a link — without faking region or user counts."""
    n = normalize("datadog", _stellar_lag_monitor_payload())

    # Classification unchanged.
    assert n.business_action == "stellar_lag"
    assert n.provider == "stellar"

    # Service from consumer_group_id; endpoint from the metric, NOT /event/event.
    assert n.service == "stellar-cosmoem-buildtransaction"
    assert n.endpoint == "airtm.message_store.lag"
    assert n.endpoint != "/event/event"

    # The Datadog event URL is preserved separately (not lost, not the endpoint).
    assert n.url == "https://app.datadoghq.com/event/event?id=123456"
    assert n.metadata["datadog_url"] == "https://app.datadoghq.com/event/event?id=123456"

    # Tags preserved in metadata.
    assert n.metadata["category"] == "stellartransaction:command"
    assert n.metadata["consumer_group_id"] == "stellar-cosmoem-buildtransaction"
    assert n.metadata["metric"] == "airtm.message_store.lag"

    # No invented region / users.
    assert n.country is None
    assert n.user_id is None


def test_stellar_lag_monitor_tags_string_parses_multi_colon_value():
    """`category:stellartransaction:command` keeps the full value (split on first ':')."""
    from app.normalizers.base import _parse_tags
    tags = _parse_tags("category:stellartransaction:command,consumer_group_id:stellar-cosmoem-buildtransaction,monitor")
    assert tags["category"] == "stellartransaction:command"
    assert tags["consumer_group_id"] == "stellar-cosmoem-buildtransaction"


def test_non_stellar_datadog_monitor_is_unchanged():
    """A plain withdrawal monitor must keep its existing classification."""
    n = normalize("datadog", {
        "title": "[Triggered] Withdrawal success rate dropped in MX",
        "message": "Withdrawal success rate fell below threshold",
        "alert_type": "error",
        "url": "https://api.airdrive.com/api/v1/withdraw/confirm",
        "tags": ["service:payments-api", "env:production", "country:MX",
                 "provider:stripe", "payment_method:card"],
    })
    assert n.business_action == "withdrawal_failed"
    assert n.provider == "stripe"
    assert n.country == "MX"
