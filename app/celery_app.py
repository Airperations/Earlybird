"""
Earlybird — Celery Configuration
Redis as broker and result backend.
"""

from celery import Celery
from app.config import settings

celery_app = Celery(
    "earlybird",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=["app.workers.process_event", "app.workers.freshdesk_sync"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,           # Only ack after task completes (no lost events)
    worker_prefetch_multiplier=1,  # Fair processing
    task_routes={
        "app.workers.process_event.*": {"queue": "events"},
        "app.workers.freshdesk_sync.*": {"queue": "freshdesk"},
    },
    beat_schedule={
        "sync-freshdesk-tickets": {
            "task": "app.workers.freshdesk_sync.sync_freshdesk_tickets",
            "schedule": settings.FRESHDESK_POLL_INTERVAL_SECONDS,
        },
    },
)
