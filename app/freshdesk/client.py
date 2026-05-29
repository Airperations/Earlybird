"""
Earlybird — Freshdesk Client & Ticket Syncer
Polls Freshdesk API and stores tickets for comparison.
"""

import requests
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, List
from app.config import settings

logger = logging.getLogger(__name__)


class FreshdeskClient:
    """Simple Freshdesk API v2 client."""

    def __init__(self):
        self.base_url = f"https://{settings.FRESHDESK_DOMAIN}/api/v2"
        self.auth = (settings.FRESHDESK_API_KEY, "X")
        self.session = requests.Session()
        self.session.auth = self.auth
        self.session.headers.update({"Content-Type": "application/json"})

    def get_tickets(
        self,
        since: Optional[datetime] = None,
        page: int = 1,
        per_page: int = 100,
    ) -> List[dict]:
        """
        Fetch recent tickets from Freshdesk.
        Filters by updated_since to avoid re-processing old tickets.
        """
        if not settings.FRESHDESK_DOMAIN or not settings.FRESHDESK_API_KEY:
            logger.warning("[FRESHDESK] Not configured — skipping ticket sync")
            return []

        params = {
            "page": page,
            "per_page": per_page,
            "order_by": "created_at",
            "order_type": "desc",
        }

        if since:
            params["updated_since"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")

        try:
            response = self.session.get(
                f"{self.base_url}/tickets",
                params=params,
                timeout=10,
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.error(f"[FRESHDESK] API error: {e}")
            return []

    def get_ticket(self, ticket_id: str) -> Optional[dict]:
        """Fetch a single ticket by ID."""
        try:
            response = self.session.get(
                f"{self.base_url}/tickets/{ticket_id}",
                timeout=10,
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.error(f"[FRESHDESK] Failed to get ticket {ticket_id}: {e}")
            return None


freshdesk_client = FreshdeskClient()
