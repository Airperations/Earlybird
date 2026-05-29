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
    SLACK_ALERT_CHANNEL: str = "#earlybird-alerts"

    # Freshdesk
    FRESHDESK_DOMAIN: str = ""           # e.g. "airdrive.freshdesk.com"
    FRESHDESK_API_KEY: str = ""

    # Anthropic (Claude Haiku)
    ANTHROPIC_API_KEY: str = ""
    LLM_MODEL: str = "claude-haiku-4-5-20251001"

    # Webhook secrets (for signature / shared-secret validation)
    SENTRY_WEBHOOK_SECRET: Optional[str] = None
    DATADOG_WEBHOOK_SECRET: Optional[str] = None
    PRODUCT_WEBHOOK_SECRET: Optional[str] = None
    FRESHDESK_WEBHOOK_SECRET: Optional[str] = None

    # Scoring thresholds
    CRITICAL_SCORE_THRESHOLD: int = 100
    HIGH_SCORE_THRESHOLD: int = 80
    MEDIUM_SCORE_THRESHOLD: int = 60
    LOW_SCORE_THRESHOLD: int = 40

    # Deduplication
    DEDUP_WINDOW_MINUTES: int = 30

    # Freshdesk matching
    FRESHDESK_MATCH_WINDOW_HOURS: int = 24
    FRESHDESK_POLL_INTERVAL_SECONDS: int = 60

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


settings = Settings()
