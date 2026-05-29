# 🛡️ Earlybird

**Early Incident Detection & Support Benchmarking System**

An autonomous agent that monitors every error and anomaly on the Airtm platform, detects them before they become support tickets, and proves it with an immutable audit trail.

---

## 🏆 Success Metric

> **Agent Win Rate ≥ 80% over a 30-day trial**
>
> Win = Agent alert timestamp < First related Freshdesk ticket timestamp

The dashboard shows this in real-time. No manual counting.

---

## Architecture

```
Sentry / Datadog / Product Events
        ↓ (milliseconds)
FastAPI Ingestion — immediate timestamp + HTTP 200
        ↓
Redis + Celery Queue — durable async processing
        ↓
Event Normalizer → Fingerprinting → Deduplication
        ↓
Business Criticality Matrix (6 scoring factors)
        ↓  (if score ≥ threshold)
Claude Haiku LLM — incident summary in <2s
        ↓
Slack Alert ← AGENT ALERT TIMESTAMP LOCKED HERE
        ↓
PostgreSQL Audit Log (immutable evidence)
        ↓
Freshdesk Comparator (polling + real-time webhook)
        ↓
Victory Dashboard — Agent Won / Lost / Lead Time
```

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

# 4. Run the demo simulation
python simulate_demo.py
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

**API service** → Settings → Start Command:
```bash
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

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
2. Agent captures timestamp immediately
3. Celery worker normalizes and scores the event (score: ~150 → Critical)
4. Claude Haiku generates an incident summary
5. Slack alert sent with `agent_alert_timestamp` locked
6. A Freshdesk ticket arrives 3 minutes later
7. Matcher records: **Agent Won (+180s lead time)**
8. Dashboard shows win rate

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

---

## Key Design Decisions

**Why Redis + Celery instead of FastAPI BackgroundTasks?**
For a 30-day production trial, task durability matters. If the server restarts, BackgroundTasks are lost. Celery with `acks_late=True` guarantees no event is dropped.

**Why Claude Haiku?**
Fast (< 2s), cheap, and produces JSON reliably. The LLM only runs on high-confidence incidents — not on every error — so costs stay low.

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
│   ├── models.py                # Full DB schema (6 tables)
│   ├── celery_app.py            # Celery + Beat config
│   ├── webhooks/
│   │   ├── sentry.py            # Sentry webhook receiver
│   │   ├── datadog.py           # Datadog webhook receiver
│   │   └── product_events.py    # Custom product events
│   ├── normalizers/
│   │   └── base.py              # Sentry/Datadog/Product normalizer
│   ├── incidents/
│   │   ├── scoring.py           # Business criticality matrix
│   │   └── service.py           # Incident state machine
│   ├── llm/
│   │   └── analyst.py           # Claude Haiku integration
│   ├── alerts/
│   │   └── slack.py             # Rich Slack Block Kit alerts
│   ├── freshdesk/
│   │   ├── client.py            # Freshdesk API client
│   │   ├── matcher.py           # Race result calculator
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
| GET | `/dashboard/summary` | Win rate and lead time metrics |
| GET | `/dashboard/incidents` | Full incident comparison log |
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
