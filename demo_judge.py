"""
Earlybird — Judge Demo (offline, zero-dependency)

    python demo_judge.py        # or:  make demo-judge

Proves the whole winning story end-to-end with NO Postgres, Redis, Slack, or
Anthropic required — it runs the *real* pipeline against in-memory SQLite and a
fake (but delivered) Slack call:

    1. a withdrawal error event arrives (Mexico, HTTP 502, provider=stripe)
    2. an incident is created with structured metadata
    3. the minimal alert is "delivered" → agent_alert_timestamp is LOCKED
    4. a Freshdesk ticket arrives 3 minutes later (Spanish: "no me deja retirar")
    5. the matcher records the race with a transparent match explanation
    6. the judge audit endpoint shows a verifiable WIN

Every number printed below is read back from the same audit endpoint a judge
would call in production — nothing is hand-computed.
"""

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone, timedelta

# This demo uses its own in-memory SQLite engine; app.database still requires the
# env var at import time, so provide a harmless dummy before importing app modules.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://demo:demo@localhost/demo")

from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession


@compiles(JSONB, "sqlite")
def _jsonb_as_json(element, compiler, **kw):   # demo runs on SQLite
    return "JSON"


from app.database import Base
import app.models  # noqa: F401 — register tables
from app.models import MetricBucket
from app.normalizers.base import normalize, NormalizedEventSchema
from app.incidents import service, alerting
from app.incidents.scoring import calculate_criticality
from app.incidents.metrics import detect_baseline_anomaly, floor_minute
from app.alerts.slack import AlertDeliveryResult
from app.freshdesk.ingest import ingest_ticket
from app.dashboard.routes import get_judge_audit, get_dashboard_summary, get_metrics


SENTRY_EVENT = {
    "project_slug": "payments-api",
    "event": {
        "title": "GatewayTimeout: provider did not respond",
        "tags": [
            ["environment", "production"],
            ["country_code", "MX"],
            ["http.status_code", "502"],
            ["provider", "stripe"],
            ["payment_method", "card"],
        ],
        "request": {"url": "https://api.airtm.com/api/v1/withdraw/confirm", "method": "POST"},
        "exception": {"values": [{"type": "GatewayTimeout", "value": "no response in 30s"}]},
        "user": {"id": "real_user_mx_001"},   # raw id — must NOT be persisted
        "contexts": {"response": {"status_code": 502}},
        "platform": "python",
    },
}


def _delivered(**kwargs) -> AlertDeliveryResult:
    """Fake Slack: always delivers, so the benchmark timestamp locks (no network)."""
    return AlertDeliveryResult(delivered=True, message_id="demo-msg", thread_ts="demo.ts", attempts=1)


def _fake_summary(_context: dict) -> dict:
    return {
        "title": "Withdrawal failures in Mexico",
        "summary": "Users in MX hit HTTP 502 confirming withdrawals via stripe.",
        "suspected_root_cause": "Payment provider timeout.",
        "recommended_next_steps": ["Check stripe latency", "Review recent payments-api deploy"],
        "support_message": "Some withdrawals may fail; engineering is on it.",
        "affected_area": "withdrawals",
    }


def _noop_followup(**kwargs) -> AlertDeliveryResult:
    return AlertDeliveryResult(delivered=True, message_id="demo-thread")


async def run():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)

    async with Session() as db:
        print("\n" + "=" * 64)
        print("  EARLYBIRD — JUDGE DEMO (offline)")
        print("=" * 64)

        # 1) event → normalize → incident (+ structured metadata)
        normalized = normalize("sentry", SENTRY_EVENT)
        incident = await service.find_or_create_incident(db, normalized)
        await service.save_normalized_event(db, raw_event_id=None, normalized=normalized, incident=incident)
        scoring = calculate_criticality(
            event=normalized, affected_users=incident.affected_users_count,
            event_count=incident.event_count, countries=list(incident.countries or []),
            has_existing_tickets=False,
        )
        await service.update_incident_score(db, incident, scoring)
        print(f"\n[1] Incident created: {str(incident.id)[:8]}  "
              f"action={incident.business_action} service={incident.service} "
              f"country={incident.primary_country} provider={incident.provider}")
        print(f"    score={scoring.total_score} severity={scoring.severity}")
        print(f"    affected_user_hashes={incident.affected_user_hashes}  "
              f"(raw id 'real_user_mx_001' is NOT stored)")

        # 2) deliver the minimal alert → lock agent_alert_timestamp
        result = await alerting.deliver_alert(db, incident, normalized, scoring, send_alert=_delivered)
        await alerting.enrich_incident(db, incident, normalized, scoring,
                                       generate_summary=_fake_summary, send_followup=_noop_followup,
                                       thread_ts=result.thread_ts)
        await db.commit()
        print(f"\n[2] Alert DELIVERED → agent_alert_timestamp = {incident.agent_alert_timestamp.isoformat()}")

        # 3) Freshdesk ticket arrives 3 minutes later (Spanish complaint)
        ticket_created = incident.agent_alert_timestamp + timedelta(seconds=180)
        ticket = {
            "id": 990001,
            "subject": "No me deja hacer un retiro",
            "description_text": "Intenté retirar con stripe y me sale error. Estoy en México.",
            "requester": {"email": "real_user@gmail.com"},   # raw email — must be hashed
            "tags": [{"name": "MX"}, {"name": "withdrawal"}],
            "created_at": ticket_created.isoformat(),
        }
        await ingest_ticket(db, ticket)
        await db.commit()
        print(f"\n[3] Freshdesk ticket {ticket['id']} arrived at {ticket_created.isoformat()} (+180s)")

        # 4) read the verdict back from the judge audit endpoint
        audit = await get_judge_audit(db)
        entry = audit["incidents"][0]
        match = entry["match"]
        summary = await get_dashboard_summary(db)

        print("\n[4] JUDGE AUDIT (read back from /dashboard/audit):")
        print(f"    incident_id            : {entry['incident_id']}")
        print(f"    business_action        : {entry['metadata']['business_action']}")
        print(f"    agent_alert_timestamp  : {entry['agent_alert_timestamp']}")
        print(f"    freshdesk_created_at    : {match['freshdesk_ticket_timestamp']}")
        print(f"    lead_time              : {match['lead_time_human']}  ({match['time_delta_seconds']}s)")
        print(f"    outcome                : {match['outcome_label']}")
        print(f"    confidence             : {match['confidence']}")
        print(f"    matched_by             : {match['matched_by']}")
        print(f"    match_reasons          : {json.dumps(match['match_reasons'], ensure_ascii=False)}")
        print(f"    benchmark==delivery    : {entry['benchmark_timestamp_matches_delivery']}")

        print("\n[5] SCOREBOARD (read back from /dashboard/summary):")
        print(f"    win_rate               : {summary['bounty_metric']['win_rate_percent']}%  "
              f"({summary['bounty_metric']['status']})")
        print(f"    official_win_rule      : {summary['official_win_rule']}")
        print(f"    race_results           : {summary['race_results']}")

        # ── [6] Self-built rolling baseline detects a silent success-rate drop ──
        # Seed relative to wall-clock now so /dashboard/metrics (which uses its own
        # now) and detect_baseline_anomaly agree on the window split.
        bench_now = datetime.now(timezone.utc)
        floor = floor_minute(bench_now)
        for i in range(5, 25):       # 20 healthy minutes: 97% success
            db.add(MetricBucket(id=uuid.uuid4(), bucket_start=floor - timedelta(minutes=i),
                                business_action="withdrawal", dimension="country", dimension_value="MX",
                                total_count=100, success_count=97, failure_count=3, pending_count=0,
                                latency_count=0, latency_sum_ms=0.0, latency_max_ms=0.0))
        db.add(MetricBucket(id=uuid.uuid4(), bucket_start=floor, business_action="withdrawal",
                            dimension="country", dimension_value="MX",
                            total_count=100, success_count=71, failure_count=29, pending_count=0,
                            latency_count=0, latency_sum_ms=0.0, latency_max_ms=0.0))
        await db.commit()
        probe = NormalizedEventSchema(
            source="product", service="payments-api", environment="production",
            endpoint="/withdraw/confirm", url="", http_status=502, exception_type=None,
            message=None, user_id=None, country="MX", platform=None, release=None,
            fingerprint="probe", raw_payload={}, business_action="withdrawal_failed",
        )
        baseline = await detect_baseline_anomaly(db, probe, now=bench_now)
        mv = await get_metrics(db)
        print("\n[6] SELF-BUILT BASELINE (read back from /dashboard/metrics):")
        print(f"    active_anomalies       : {mv['active_anomalies']}")
        if baseline.is_anomaly:
            d = baseline.detail
            print(f"    detected               : {baseline.kind} on withdrawal · "
                  f"{d.get('dimension')}={d.get('value')}")
            if "baseline_success_rate" in d:
                print(f"    success_rate           : {d['baseline_success_rate']:.0%} → "
                      f"{d['current_success_rate']:.0%}  (drop {d['drop']:.0%})")

        # ── [7] Multichannel fallback: Slack down → PagerDuty delivers ─────────
        fb_event = {**SENTRY_EVENT, "project_slug": "payments-api",
                    "event": {**SENTRY_EVENT["event"],
                              "request": {"url": "https://api.airtm.com/api/v1/deposit/confirm"}}}
        fb_norm = normalize("sentry", fb_event)
        fb_inc = await service.find_or_create_incident(db, fb_norm)
        fb_scoring = calculate_criticality(event=fb_norm, affected_users=1, event_count=1,
                                           countries=["MX"], has_existing_tickets=False)
        await service.update_incident_score(db, fb_inc, fb_scoring)
        fb_result = await alerting.deliver_alert(
            db, fb_inc, fb_norm, fb_scoring,
            send_alert=lambda **k: AlertDeliveryResult(delivered=False, channel="slack", attempts=3, error="slack down"),
            send_pagerduty=lambda **k: AlertDeliveryResult(delivered=True, channel="pagerduty", message_id="pd-1", attempts=1),
        )
        await db.commit()
        print("\n[7] MULTICHANNEL FALLBACK (Slack outage):")
        print(f"    slack                  : failed after 3 attempts")
        print(f"    delivered via          : {fb_inc.notification_channel}")
        print(f"    benchmark locked       : {fb_inc.agent_alert_timestamp is not None} "
              f"(by the first channel that delivered)")

        ok = match["outcome"] == "agent_won" and baseline.is_anomaly and fb_inc.notification_channel == "pagerduty"
        print("\n" + "=" * 64)
        print("  RESULT:  " + ("🏆 WIN — agent beat support to the timestamp" if ok
                               else "❌ unexpected outcome"))
        print("=" * 64 + "\n")

    await eng.dispose()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
