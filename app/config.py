"""
Earlybird — Configuration
All settings loaded from environment variables.
"""

from pydantic_settings import BaseSettings
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

    # Webhook secrets (for signature validation)
    SENTRY_WEBHOOK_SECRET: Optional[str] = None
    DATADOG_WEBHOOK_SECRET: Optional[str] = None

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

    class Config:
        env_file = ".env"
        case_sensitive = True


settings = Settings()
