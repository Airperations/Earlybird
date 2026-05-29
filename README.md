# 🛡️ Earlybird

**Early Incident Detection & Support Benchmarking System**

An autonomous agent that monitors every error and anomaly on the Airtm platform, detects them before they become support tickets, and proves it with an immutable audit trail.

_Built by **Angy Duarte** — [angy@airtm.io](mailto:angy@airtm.io)_

---

## 🏆 Success Metric

> **Agent Win Rate ≥ 80% over a 30-day trial**
>
> Win = Agent alert timestamp < First related Freshdesk ticket timestamp

The dashboard shows this in real-time. No manual counting.

---

## 🥇 Why this wins

The challenge is decided on a single comparison: **agent alert timestamp vs. Freshdesk ticket timestamp.** Every design choice below exists to win that race honestly.

1. **The agent alerts immediately — *before* LLM enrichment.** The pipeline is `score → minimal immediate alert → real delivered timestamp → LLM enrichment → thread follow-up`. The first Slack message carries everything a responder needs (id, fingerprint, score, severity, affected users, event count, first/last seen, endpoint/service/action, region/platform, status `enriching…`) and is sent with **zero LLM latency** in front of it. Claude's analysis arrives seconds later as a threaded reply. _Enforced by tests in `tests/test_alerting.py` and `tests/test_slack_retry.py`._

2. **The official benchmark timestamp is set only after *real* notification delivery.** `agent_alert_timestamp` is assigned in exactly one place — `mark_incident_delivered()` — mirroring `notification_delivered_at`. A Slack failure (after retries) moves the incident to `notification_failed` and leaves the timestamp **NULL**. The agent can never claim a win it didn't deliver. _Enforced by `test_official_timestamp_set_only_after_delivery` and `test_failed_delivery_records_no_timestamp`._

3. **Freshdesk webhooks are processed immediately.** `/freshdesk/webhook` persists the ticket **and runs the matcher inside the request** — the race is scored the instant a ticket is created, not on the next 60s poll. _Enforced by `test_webhook_immediate_save_and_match`._

4. **Matching is a metadata / time / keyword / semantic hybrid — never LLM-only, and fully explained.** Every match records a structured, auditable `matched_by` (`["business_action", "country", "provider", "time_window", "keyword_match"]`) and `match_reasons` (the matched country/provider, the multilingual `keyword_overlap`, the detected `keyword_language` of `en`/`es`/`mixed`, and the signed `time_delta_seconds`). Keyword groups are organised by meaning across languages, so the label stays generic while the language is reported, not hardcoded. A model hallucination alone can't fabricate or destroy a match. _See `app/freshdesk/matcher.py::explain_match` and `app/taxonomy.py`._

5. **Each incident describes itself without the LLM.** Incidents carry structured metadata — `service`, `endpoint`, `route`, `business_action` (e.g. `withdrawal_failed`), `http_status`, `exception_type`, `primary_country`, `provider`, `platform`, `payment_method`, `normalized_keywords` — so the matcher and a human auditor can answer *what / where / who* from columns, not prose.

6. **Every recurrence is its own benchmark race.** `fingerprint` is no longer globally unique; a partial unique index allows only **one open incident per fingerprint**. A resolved/false-positive fingerprint that reappears becomes a **new incident row** with a clean id, `first_seen`, alert timestamp, and Freshdesk comparison — so recurrences never mix histories. _Enforced by `tests/test_fingerprint_recurrence.py`._

7. **PII never lands in the database, Slack, the LLM, or logs.** User/account ids are stored as salted one-way hashes (`affected_user_hashes`, never `affected_user_ids`); emails are hashed; raw webhook payloads are scrubbed (ids hashed, emails/secrets/phones masked) before the forensic copy is persisted. _Enforced by `tests/test_pii_hashing.py`._

8. **Anomaly detection catches silent business degradation before users complain.** Z-score volume spikes, elevated failure rates, stuck/pending rates, and latency regressions trip an alert even when no single error looks severe — and critical money-flows alert on tiny samples. _See `app/incidents/anomaly.py`._

9. **The judge audit endpoint proves wins transparently — and shows where it lost.** `GET /dashboard/audit` returns, per incident, the full lifecycle timeline, structured metadata, the benchmark timestamp, the matched ticket with signed delta + `matched_by`/`match_reasons`, and the **immutable** audit-log trail. `GET /dashboard/summary` adds honest counters: **p90 lead time**, **false positives**, **unmatched Freshdesk tickets**, and **detected-but-delivery-failed** — losses and gaps are surfaced, never hidden. The official rule stays strict: `agent_alert_timestamp < freshdesk_ticket_created_at`.

---

## How it works

### Where does incident data come from?

Earlybird does not connect to Sentry or Datadog — it works the other way around. When Sentry detects an error, it automatically sends an HTTP POST to Earlybird's webhook URL with all the context already included:

```json
{
  "tags": [
    ["country_code", "MX"],
    ["environment", "production"],
    ["http.status_code", "502"]
  ],
  "request": {
    "url": "https://api.airtm.com/api/v1/withdraw/confirm"
  },
  "user": { "id": "user_001" }
}
```

The country, endpoint, user, error type — all of it comes from Sentry automatically. Earlybird just needs to receive it, which only requires adding one webhook URL in Sentry's settings. No credentials, no API keys, no access to production systems needed.

### What does Claude Haiku do exactly?

Claude Haiku has one job: turn raw technical data into clear, human-readable summaries that engineers and support can act on immediately.

It receives the processed incident:

```json
{
  "endpoint": "/withdraw/confirm",
  "http_status": 502,
  "affected_users": 12,
  "countries": ["MX", "CO"],
  "exception_type": "GatewayTimeout"
}
```

And returns this:

```json
{
  "title": "Withdrawal failures in LATAM",
  "summary": "Users in Mexico and Colombia are receiving HTTP 502 errors during withdrawal confirmation.",
  "suspected_root_cause": "Payment provider timeout or recent deployment in payments-api.",
  "recommended_next_steps": [
    "Check recent deployments in payments-api",
    "Inspect payment provider latency",
    "Review withdrawal confirmation logs",
    "Notify support proactively"
  ],
  "support_message": "Some users may experience failed withdrawal confirmations. Engineering is investigating."
}
```

Claude Haiku does not detect the incident — that is done by the scoring matrix. Claude only explains it in plain language so the team understands what happened without reading logs. It is only called for high-confidence incidents above the score threshold, keeping costs low.

### What the Slack alert looks like

Alerting is **two-phase**, and this ordering is the whole game:

1. **Immediate minimal alert** (`send_immediate_alert`) — fired the instant the incident crosses threshold, with **no LLM call in front of it**. It shows status `enriching…` and carries id, fingerprint, score, severity, impact, region, endpoint/service/action, platform, and first/last-seen timestamps. Its **delivery** is what locks `agent_alert_timestamp`.
2. **Enrichment follow-up** (`send_enrichment_followup`) — Claude's analysis, posted seconds later as a **thread reply** (with a bot token) or a follow-up message (with a webhook). If the LLM fails, the incident still counts as alerted.

The enriched view (delivered as the thread reply) is built in `app/alerts/slack.py` and looks like this:

```
┌────────────────────────────────────────────────────────────┐  ← colored bar
│ 🔴 EARLYBIRD — CRITICAL INCIDENT                             │     (red = critical)
│ Withdrawal failures in LATAM                                 │
├──────────────────────────────────────────────────────────── │
│ Service:  payments-api      Endpoint:  /withdraw/confirm     │
│ Impact:   12 users, 47 errors   Region:  MX, CO              │
│ Score:    125 / critical    Owner:   payments               │
│                                                              │
│ Guardian Analysis:                                           │
│   Users in Mexico and Colombia are receiving HTTP 502        │
│   errors during withdrawal confirmation.                     │
│                                                              │
│ Suspected Root Cause:                                        │
│   Payment provider timeout or recent payments-api deploy.    │
│                                                              │
│ Suggested Next Steps:                                        │
│   1. Check recent deployments in payments-api                │
│   2. Inspect payment provider latency                        │
│   3. Review withdrawal confirmation logs                     │
│                                                              │
│ Support Message:                                             │
│   Some users may experience failed withdrawals. Engineering  │
│   is investigating.                                          │
├──────────────────────────────────────────────────────────── │
│ ⏱️ Agent Alert Timestamp: 2026-05-28T20:43:01Z               │
│ 🆔 Incident: 3f9a1c2b   🔍 Fingerprint: a1b2c3d4e5f6...      │
│ 📋 Freshdesk: Monitoring...                                  │
└────────────────────────────────────────────────────────────┘
```

Everything in that message is generated automatically when the webhook fires — zero manual intervention:

| Element | Source |
|---------|--------|
| **Colored bar** (red/orange/yellow/blue) | Severity from the scoring matrix |
| **Guardian Analysis** | Claude Haiku, in real time |
| **Suspected root cause** | Inferred by Claude from the error context |
| **Suggested next steps** | Claude, specific to the incident type |
| **Support message** | Claude — ready to copy and send to users |
| **Agent Alert Timestamp** | The exact moment locked for the Freshdesk race |
| **Freshdesk: Monitoring…** | Flips to `🏆 WON +3m 11s` (or `❌ LOST`) once a matching ticket is found |

PII (emails, account numbers, secrets) is redacted before the text is sent to Claude or rendered to Slack (see `app/redaction.py`). If `SLACK_WEBHOOK_URL` is not configured, the alert is skipped gracefully and the incident is still recorded with its timestamp.

---

## Architecture

```
Webhook / Product Events / Metrics   (Sentry · Datadog · Airtm platform)
        ↓ (milliseconds)
FastAPI Ingestion — immediate timestamp + HTTP 200
        ↓
Redis + Celery Queue — durable async processing
        ↓
Normalizer → Fingerprinting → Deduplication / Recurrence
        ↓
Scoring + Anomaly Detection   (criticality matrix · z-score / failure / pending / latency)
        ↓  (if score ≥ threshold  OR  anomaly  OR  critical money-flow)
Immediate Alert Delivery  ──►  AGENT ALERT TIMESTAMP LOCKED ON DELIVERY
        ↓                       (retry + backoff; failure ⇒ no timestamp)
LLM Enrichment            ──►  posted as a Slack thread reply (after delivery)
        ↓
Freshdesk Matching        (immediate webhook ingest · hybrid confidence)
        ↓
Judge Audit Dashboard     — Won / Lost / Lead Time + immutable evidence trail
```

The benchmark timestamp is locked at **Immediate Alert Delivery**, two steps
*before* LLM enrichment — so the agent's clock starts as early as physically
possible while the AI write-up still arrives moments later in-thread.

---

## Quick Start

```bash
# 1. Clone and configure
git clone <repo-url>
cd earlybird
cp .env.example .env
# Fill in your SLACK_WEBHOOK_URL, ANTHROPIC_API_KEY, FRESHDESK_* keys

# 2. Start everything
docker-compose up --build

# 3. Open dashboards
# API docs:  http://localhost:8000/docs
# Dashboard: http://localhost:8501

# 4. Prove a WIN end-to-end with no cloud services (offline)
make demo-judge        # runs the real pipeline on SQLite + a fake delivered alert

# 5. Or run the live HTTP demo against a running API
python simulate_demo.py
```

### One-command workflows (Makefile)

```bash
make install      # pip install -r requirements.txt
make test         # full suite (fast, in-memory SQLite — no Postgres/Redis needed)
make migrate      # alembic upgrade head (needs Postgres)
make demo-judge   # offline end-to-end proof of a WIN, read back from the audit endpoint
make up           # docker-compose up --build (full stack)
```

The suite proves the winning invariants directly: alert-before-LLM, timestamp-only-after-delivery, Slack retry, anomaly detection, **self-built rolling-baseline detection (MetricBucket)**, **multichannel fallback (Slack→PagerDuty→email)**, **structured match explanations (`matched_by`/`match_reasons`, multilingual)**, immediate Freshdesk ingest, state-machine transitions, **recurrence-as-a-new-race + the partial unique index**, **PII hashing (no raw user ids stored)**, **honesty metrics (p90 / false-positives / unmatched / detected-but-undelivered)**, and PII redaction.

### Running the app locally (without Docker)

```bash
alembic upgrade head                                              # migrate (Postgres)
uvicorn app.main:app --reload                                     # API
celery -A app.celery_app worker --loglevel=info -Q events,freshdesk   # worker
celery -A app.celery_app beat --loglevel=info                    # scheduler
```

---

## 🚂 Deploy to Railway (Production — 24/7)

Railway runs Earlybird in the cloud so it works even when your computer is off.

### Step 1 — Create account
Go to [railway.app](https://railway.app) → **Login with GitHub**

### Step 2 — New project
**New Project** → **Deploy from GitHub repo** → select `Earlybird`

### Step 3 — Add databases
Inside the project:
- **+ New** → **Database** → **Add PostgreSQL**
- **+ New** → **Database** → **Add Redis**

Railway generates the connection URLs automatically.

### Step 4 — Set environment variables
Click your Earlybird service → **Variables** tab → add:

```env
APP_ENV=production
DATABASE_URL=<copy from Railway PostgreSQL service>
REDIS_URL=<copy from Railway Redis service>
SLACK_WEBHOOK_URL=your_slack_webhook
SLACK_ALERT_CHANNEL=#earlybird-alerts
ANTHROPIC_API_KEY=your_key
LLM_MODEL=claude-haiku-4-5-20251001
FRESHDESK_DOMAIN=airtm.freshdesk.com
FRESHDESK_API_KEY=your_key
FRESHDESK_POLL_INTERVAL_SECONDS=60
FRESHDESK_MATCH_WINDOW_HOURS=24
CRITICAL_SCORE_THRESHOLD=100
HIGH_SCORE_THRESHOLD=80
MEDIUM_SCORE_THRESHOLD=60
LOW_SCORE_THRESHOLD=40
DEDUP_WINDOW_MINUTES=30
```

### Step 5 — Configure the 3 services

**API service** → Settings:

- **Pre-Deploy Command** (runs migrations once, before the app starts — keeps them
  out of the healthcheck window so a transactional migration can't be killed
  mid-run and leave Postgres locks):
  ```bash
  alembic upgrade head
  ```
- **Start Command** (only serves traffic). Use the plain form — Railway substitutes
  `$PORT` itself, so no `sh -c` wrapper and no quotes (quotes break Railway's command
  parser; the `${VAR:-default}` syntax is also unsupported):
  ```bash
  uvicorn app.main:app --host 0.0.0.0 --port $PORT
  ```

> Set the Pre-Deploy Command on the **API service only** — not on worker/beat —
> so the migration runs exactly once per deploy and never concurrently.

**Worker service** (new GitHub deploy from same repo) → Start Command:
```bash
celery -A app.celery_app worker --loglevel=info -Q events,freshdesk
```

**Beat service** (new GitHub deploy from same repo) → Start Command:
```bash
celery -A app.celery_app beat --loglevel=info
```

### Step 6 — Get your public URL
API service → **Settings** → **Domains** → **Generate Domain**

```
https://earlybird-production.up.railway.app
```

### Step 7 — Configure these webhook URLs in your observability tools
```
Sentry:    https://earlybird-production.up.railway.app/webhooks/sentry/
Datadog:   https://earlybird-production.up.railway.app/webhooks/datadog/
Freshdesk: https://earlybird-production.up.railway.app/freshdesk/webhook
Dashboard: https://earlybird-production.up.railway.app/dashboard/summary
```

---

## Demo Scenario

Run `python simulate_demo.py` to see the full pipeline in action:

1. A Sentry webhook fires (withdrawal error, Mexico, HTTP 502)
2. Agent captures the receive timestamp immediately and returns HTTP 200
3. Celery worker normalizes and scores the event (score: 125 → Critical)
4. **Minimal Slack alert is sent first (no LLM)** — `agent_alert_timestamp` locks on delivery
5. Claude Haiku enriches the incident; the analysis is posted as a thread reply
6. A Freshdesk ticket arrives 3 minutes later → `/freshdesk/webhook` matches it instantly
7. Matcher records: **Agent Won (+180s lead time)**
8. Dashboard `/dashboard/summary` and `/dashboard/audit` show the proof

---

## Scoring Matrix

Each incident is scored across 6 dimensions:

| Factor | Max Points |
|--------|-----------|
| Critical path (`/withdraw`, `/deposit`) | +50 |
| Affected users (100+ users) | +60 |
| Error velocity (10x growth) | +60 |
| HTTP status (5xx, timeouts) | +35 |
| Country concentration (LATAM key markets) | +20 |
| No support ticket yet | +20 |

**Thresholds:** `observe < 40 < low < 60 < medium < 80 < high < 100 < critical`

An incident alerts when its score crosses `INCIDENT_ALERT_THRESHOLD` (default 60), or `CRITICAL_BUSINESS_ACTION_THRESHOLD` (default 40) on a money-flow endpoint, **or** when an anomaly trips — so a low-volume but high-impact financial failure still beats support.

### Anomaly detection (beyond the matrix)

Some degradations never throw a loud error. `app/incidents/anomaly.py` adds pure, tested detectors that run on product-event metric payloads:

| Detector | Trips when | Tuning knob |
|----------|-----------|-------------|
| Volume spike | z-score ≥ threshold vs baseline series | `ANOMALY_Z_SCORE_THRESHOLD`, `ANOMALY_MIN_SAMPLE_SIZE` |
| Failure rate | failures/total ≥ threshold | `ANOMALY_FAILURE_RATE_THRESHOLD` |
| Pending/stuck rate | pending/total ≥ threshold | `ANOMALY_PENDING_RATE_THRESHOLD` |
| Latency regression | p95 ≥ factor × baseline p95 | `ANOMALY_LATENCY_REGRESSION_FACTOR` |

Critical money-flows use `ANOMALY_CRITICAL_MIN_SAMPLE_SIZE` so even a handful of failed withdrawals is enough to fire.

### Self-built rolling baselines (MetricBucket)

The agent doesn't wait for a producer to compute rates — it builds its **own** per-minute, per-dimension metrics from the event stream in the `metric_buckets` table, then compares a recent window to its **own preceding baseline** per `country / provider / platform / payment_method`. This is what catches *silent* degradation:

> `withdrawal` success rate in `MX` dropped **97% → 71%** · `deposit` p95 latency **3.8×** baseline · pending `transfer` rate **5σ** above normal

The statistics reuse the pure detectors above (`app/incidents/metrics.py` builds the series and calls them). `GET /dashboard/metrics` exposes the live current-vs-baseline view and any active anomalies. Business actions recognised include `withdrawal`, `direct_withdraw`, `deposit`, `transfer`, `p2p`, `payment`, `balance`, `login`, `signup`, `virtual_account`, and `virtual_card` (see `app/taxonomy.py`).

### Multichannel delivery (Slack → PagerDuty → email)

For a 30-day trial a single channel isn't enough. Delivery falls through **Slack → PagerDuty → email**, and the **first channel that confirms delivery locks the official benchmark timestamp**. The delivering channel is recorded on the incident (`notification_channel`) and the full per-channel attempt log goes to the immutable audit trail — so a Slack outage doesn't cost the agent a win it could have delivered elsewhere. _See `app/alerts/channels.py` and `tests/test_fallback_delivery.py`._

---

## Key Design Decisions

**Why Redis + Celery instead of FastAPI BackgroundTasks?**
For a 30-day production trial, task durability matters. If the server restarts, BackgroundTasks are lost. Celery with `acks_late=True` re-delivers in-flight events after a worker crash. (Events that fail all retries are logged as dropped — wiring a dead-letter queue is the next hardening step.)

**Why alert before enrichment?**
The benchmark is won or lost on the alert timestamp. Generating an LLM summary *before* the first alert would add seconds of avoidable latency to the exact number the judges measure. So the agent sends a useful minimal alert first, locks the timestamp on delivery, and enriches afterward in-thread. The LLM is never on the critical path.

**Why is the timestamp set only on delivery?**
A win must be real. `agent_alert_timestamp` is written in one place only — on confirmed Slack delivery — so a dropped notification is recorded as `notification_failed` with no timestamp, never as a silent win. Delivery is retried with backoff first (`SLACK_MAX_RETRIES`).

**Why Claude Haiku?**
Fast, cheap, and produces JSON reliably. The LLM only runs on high-confidence incidents — not on every error, and never before the first alert — so costs and latency stay low.

**Why the Freshdesk webhook endpoint?**
Polling every 60s creates a worst-case 60s delay in registering results. The `/freshdesk/webhook` endpoint enables a real-time trigger so the comparison is recorded the instant a ticket is created.

---

## Connecting to Airtm's Stack

The agent needs one of two things to receive real events:

**Option A — Webhook (recommended):**
In Sentry: `Settings → Project → Webhooks → Add Webhook → https://your-server/webhooks/sentry/`

**Option B — Datadog monitor:**
`Manage Monitors → Edit → Notify your services → Add Webhook → https://your-server/webhooks/datadog/`

No access to production systems needed beyond adding a webhook URL.

---

## Source compatibility: Sentry and Datadog

Earlybird ships with **dedicated, tested receivers** for both Sentry and Datadog.
Compatibility is demonstrated end-to-end in `tests/test_source_compatibility.py`
(normalize → incident → metric buckets → Freshdesk match → alert), not asserted
from this README.

| Source  | Status | What works | Caveat |
|---------|--------|------------|--------|
| **Sentry**  | ✅ Fully supported (issue/error webhook) | service, endpoint, http_status, exception type/value, country, provider, payment_method, platform, release, environment, user (hashed), business_action, timestamp | Error-only stream → supports failure/volume/latency detection. True *success-rate* baselines need success events or metric counts. |
| **Datadog** | ✅ Fully supported (custom webhook recommended) | tags as **list / dict / string**, service, env, country, provider, payment_method, platform, alert_type/status, business_action, aggregate `metrics` counts fed into MetricBuckets | A bare monitor with no URL/`endpoint`/`business_action` tag won't infer a business action — the **custom payload below is recommended**. |

### Endpoints & required headers

```
POST  https://<your-server>/webhooks/sentry/
POST  https://<your-server>/webhooks/datadog/
```

| Source  | Auth header (when a secret is configured) |
|---------|-------------------------------------------|
| Sentry  | `sentry-hook-signature: <HMAC-SHA256 of the raw body>` (set `SENTRY_WEBHOOK_SECRET`) |
| Datadog | `x-webhook-token: <DATADOG_WEBHOOK_SECRET>` |

Both endpoints also honour optional replay-protection headers `x-webhook-timestamp`
and `x-webhook-nonce`. If no secret is set, the endpoint is open (logged as a
warning) so local dev keeps working. Unknown or partial payloads never 500:
malformed JSON returns `400`, and an unrecognized shape normalizes to safe
defaults (`service="unknown"`, no `business_action`) and is processed
best-effort by the worker.

### Example Sentry payload

```json
{
  "project_slug": "payments-api",
  "event": {
    "title": "GatewayTimeout: upstream did not respond",
    "level": "error",
    "platform": "python",
    "environment": "production",
    "timestamp": "2026-05-29T18:43:01Z",
    "tags": [["country_code", "MX"], ["http.status_code", "502"],
             ["provider", "stripe"], ["payment_method", "card"]],
    "request": {"url": "https://api.airdrive.com/api/v1/withdraw/confirm", "method": "POST"},
    "exception": {"values": [{"type": "GatewayTimeout", "value": "no response in 30s"}]},
    "user": {"id": "user_mx_4821"}
  }
}
```

### Example Datadog custom-webhook payload (recommended)

A custom payload lets Datadog hand Earlybird the business context and the
aggregate counts that drive the rolling baselines:

```json
{
  "alert_type": "error",
  "alert_status": "Triggered",
  "title": "[Triggered] Withdrawal success rate dropped in MX",
  "url": "https://api.airdrive.com/api/v1/withdraw/confirm",
  "timestamp": "2026-05-29T18:43:05Z",
  "tags": ["service:payments-api", "env:production", "country:MX",
           "provider:stripe", "payment_method:card", "platform:ios"],
  "metrics": {
    "total_count": 200, "success_count": 118,
    "failure_count": 78, "pending_count": 4, "p95_latency_ms": 4200
  }
}
```

`tags` may be a list of `key:value` strings (above), a JSON object
(`{"service": "payments-api", ...}`), or a comma string
(`"service:payments-api,country:MX"`) — all three are normalized identically.

### Notes (judge-safe truth)

- **Datadog custom payload is recommended.** A bare standard monitor often lacks a
  URL/endpoint, so no `business_action` can be inferred and `metrics` counts won't
  be present. Include a `url`/`endpoint`/`business_action` and a `metrics` block.
- **Success-rate baselines need success signal.** The "97% → 71%" success-rate
  drop requires success events or metric `success_count`. An error-only Sentry
  stream cannot establish a success-rate baseline by itself.
- **Error-only sources still detect failure / volume / latency.** Sentry errors
  (and Datadog `failure_count`/`p95_latency_ms`) feed failure-rate, volume-spike,
  and latency-regression detection without any success signal.

### Try it locally with curl

```bash
# 1. Start the API (Postgres + Redis must be up — `make up` brings the full stack)
uvicorn app.main:app --reload --port 8000

# 2. Send a Sentry event (no secret set → open endpoint in dev)
curl -sS -X POST http://localhost:8000/webhooks/sentry/ \
  -H 'Content-Type: application/json' \
  --data @tests/fixtures/sentry_issue_payload.json

# 3. Send a Datadog custom-webhook event
curl -sS -X POST http://localhost:8000/webhooks/datadog/ \
  -H 'Content-Type: application/json' \
  --data @tests/fixtures/datadog_monitor_payload.json

# Each returns {"status":"accepted","received_at":"…"} immediately and processes
# async. Watch results at GET /dashboard/incidents and /dashboard/audit.
```

---

## Evidence Table

```
Incident ID | Agent Alert  | Freshdesk Ticket | Lead Time | Outcome
INC-001     | 20:43:01 UTC | 20:46:12 UTC     | +3m 11s   | 🏆 WON
INC-002     | 21:12:15 UTC | 21:13:02 UTC     | +47s      | 🏆 WON
INC-003     | 02:05:00 UTC | No ticket        | Prevented | 🏆 WON
INC-004     | 09:10:20 UTC | 09:09:50 UTC     | -30s      | ❌ LOST
```

---

## Project Structure

```
earlybird/
├── app/
│   ├── main.py                  # FastAPI entry point
│   ├── config.py                # All settings
│   ├── database.py              # Async SQLAlchemy
│   ├── models.py                # Full DB schema (incidents, events, matches, audit, metric_buckets, …)
│   ├── celery_app.py            # Celery + Beat config
│   ├── webhooks/
│   │   ├── sentry.py            # Sentry webhook receiver
│   │   ├── datadog.py           # Datadog webhook receiver
│   │   └── product_events.py    # Custom product events
│   ├── normalizers/
│   │   └── base.py              # Sentry/Datadog/Product normalizer
│   ├── taxonomy.py              # Business actions + multilingual keyword groups
│   ├── incidents/
│   │   ├── scoring.py           # Business criticality matrix
│   │   ├── anomaly.py           # Pure z-score / failure / pending / latency detectors
│   │   ├── metrics.py           # MetricBucket aggregation + rolling-baseline detection
│   │   ├── alerting.py          # Fast-path orchestrator (deliver → enrich; multichannel)
│   │   └── service.py           # Incident state machine + lifecycle writes
│   ├── llm/
│   │   └── analyst.py           # Claude Haiku integration
│   ├── alerts/
│   │   ├── slack.py             # Two-phase Block Kit alerts (immediate + thread)
│   │   └── channels.py          # PagerDuty + email fallback channels
│   ├── freshdesk/
│   │   ├── client.py            # Freshdesk API client
│   │   ├── matcher.py           # Hybrid match confidence + win/loss logic
│   │   ├── ingest.py            # Immediate webhook ticket save + match
│   │   └── routes.py            # Freshdesk API endpoints
│   ├── dashboard/
│   │   └── routes.py            # Metrics API
│   └── workers/
│       ├── process_event.py     # Main Celery pipeline
│       └── freshdesk_sync.py    # Periodic Freshdesk sync
├── config/
│   └── critical_paths.yaml      # Configurable business paths
├── dashboard.py                 # Streamlit visual dashboard
├── simulate_demo.py             # End-to-end demo script
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
└── .env.example
```

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/webhooks/sentry/` | Sentry webhook receiver |
| POST | `/webhooks/datadog/` | Datadog webhook receiver |
| POST | `/events/product` | Custom product events |
| POST | `/freshdesk/webhook` | Real-time Freshdesk ticket notification |
| POST | `/freshdesk/sync` | Manual sync trigger |
| GET | `/dashboard/summary` | Win rate and lead time metrics (incl. failed notifications) |
| GET | `/dashboard/incidents` | Full incident comparison log (with lifecycle timestamps) |
| GET | `/dashboard/audit` | **Judge audit** — per-incident lifecycle, deltas & immutable trail |
| GET | `/dashboard/metrics` | Self-built rolling baselines — current vs baseline rates + live anomalies |
| GET | `/dashboard/win-rate` | Single win rate metric |
| GET | `/health` | Health check |
| GET | `/docs` | Interactive API docs |

---

## Built with

- **FastAPI** — High-performance async API
- **Celery + Redis** — Durable async processing
- **PostgreSQL** — Immutable audit trail
- **Claude Haiku (Anthropic)** — Incident analysis
- **Slack Block Kit** — Rich alert formatting
- **Streamlit** — Visual demo dashboard
- **Docker Compose** — One-command deployment
