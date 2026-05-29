"""
Earlybird — Configuration
All settings loaded from environment variables.
"""

from pydantic_settings import BaseSettings
from pydantic import field_validator
from typing import Optional


class Settings(BaseSettings):
    # App
    APP_ENV: str = "development"
    APP_NAME: str = "Earlybird"

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://user:password@localhost:5432/earlybird"

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # Slack
    SLACK_WEBHOOK_URL: str = ""
    SLACK_BOT_TOKEN: str = ""             # chat.postMessage token (enables thread replies)
    SLACK_ALERT_CHANNEL: str = "#earlybird-alerts"

    # Optional fallback notification channels (used only if Slack delivery fails).
    ALERT_FALLBACK_EMAIL: Optional[str] = None
    PAGERDUTY_ROUTING_KEY: Optional[str] = None

    # Dashboard auth — when set, /dashboard/* requires header `x-dashboard-key`.
    DASHBOARD_API_KEY: Optional[str] = None

    # Freshdesk
    FRESHDESK_DOMAIN: str = ""           # e.g. "airdrive.freshdesk.com"
    FRESHDESK_API_KEY: str = ""

    # Anthropic (Claude Haiku)
    ANTHROPIC_API_KEY: str = ""
    LLM_MODEL: str = "claude-haiku-4-5-20251001"
    LLM_ENRICHMENT_TIMEOUT_SECONDS: float = 10.0

    # Salt for hashing user identifiers before they are stored / matched.
    # Keeps user references non-PII while still allowing overlap detection.
    USER_HASH_SALT: str = "earlybird-default-salt-change-me"

    # Webhook secrets (for signature / shared-secret validation)
    SENTRY_WEBHOOK_SECRET: Optional[str] = None
    DATADOG_WEBHOOK_SECRET: Optional[str] = None
    PRODUCT_WEBHOOK_SECRET: Optional[str] = None
    FRESHDESK_WEBHOOK_SECRET: Optional[str] = None

    # ── Scoring thresholds ─────────────────────────────────────────────────
    CRITICAL_SCORE_THRESHOLD: int = 100
    HIGH_SCORE_THRESHOLD: int = 80
    MEDIUM_SCORE_THRESHOLD: int = 60
    LOW_SCORE_THRESHOLD: int = 40

    # The score an incident must cross to fire an immediate alert.
    INCIDENT_ALERT_THRESHOLD: int = 60
    # Critical business actions (e.g. withdrawal_failed) alert at a lower bar so
    # a low-volume but high-impact financial issue still beats support.
    CRITICAL_BUSINESS_ACTION_THRESHOLD: int = 40

    # Deduplication
    DEDUP_WINDOW_MINUTES: int = 30

    # ── Anomaly detection ──────────────────────────────────────────────────
    ANOMALY_Z_SCORE_THRESHOLD: float = 3.0
    ANOMALY_MIN_SAMPLE_SIZE: int = 20
    # Minimum sample size for critical business actions — small absolute counts
    # are still meaningful when money is involved.
    ANOMALY_CRITICAL_MIN_SAMPLE_SIZE: int = 3
    ANOMALY_BASELINE_WINDOW_MINUTES: int = 60
    ANOMALY_FAILURE_RATE_THRESHOLD: float = 0.30   # 30% failure rate triggers
    ANOMALY_PENDING_RATE_THRESHOLD: float = 0.40
    ANOMALY_LATENCY_REGRESSION_FACTOR: float = 2.0  # p95 doubling is a regression

    # ── Freshdesk matching ─────────────────────────────────────────────────
    FRESHDESK_MATCH_WINDOW_HOURS: int = 24
    FRESHDESK_MATCH_TIME_WINDOW_MINUTES: int = 1440  # 24h, kept as minutes knob
    FRESHDESK_MATCH_CONFIDENCE_THRESHOLD: float = 0.5
    FRESHDESK_POLL_INTERVAL_SECONDS: int = 60

    # ── Slack reliability ──────────────────────────────────────────────────
    SLACK_MAX_RETRIES: int = 3
    SLACK_RETRY_BACKOFF_SECONDS: float = 0.5
    SLACK_TIMEOUT_SECONDS: float = 5.0

    @field_validator("DATABASE_URL")
    @classmethod
    def _force_asyncpg(cls, v: str) -> str:
        """
        Railway/Heroku hand out `postgres://` or `postgresql://`. The async engine
        in database.py requires the asyncpg driver, so normalize any plain Postgres
        URL to `postgresql+asyncpg://`. A copy-pasted Railway connection string
        then Just Works without a broken startup.
        """
        if v.startswith("postgres://"):
            v = v.replace("postgres://", "postgresql://", 1)
        if v.startswith("postgresql://"):
            v = v.replace("postgresql://", "postgresql+asyncpg://", 1)
        return v

    class Config:
        env_file = ".env"
        case_sensitive = True
        extra = "ignore"


settings = Settings()
