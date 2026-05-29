"""
Earlybird — LLM Incident Analyst
Uses Claude Haiku for fast, cheap, high-quality incident summaries.
Only called for high-confidence incidents above the score threshold.
"""

import anthropic
import json
import logging
from typing import Optional
from app.config import settings
from app.redaction import redact_pii

logger = logging.getLogger(__name__)

# Bound the call: the SDK default timeout is ~10 minutes, which would block the
# single-task Celery worker and break the "<2s, alert before the ticket" promise.
# We own retries at the Celery level, so disable the SDK's internal retries too.
client = anthropic.Anthropic(
    api_key=settings.ANTHROPIC_API_KEY,
    timeout=10.0,
    max_retries=0,
)

SYSTEM_PROMPT = """You are Earlybird, an expert incident analyst for a fintech platform.

Your job is to analyze production incidents and produce clear, actionable summaries for engineering and support teams.

Always respond with valid JSON only. No markdown, no preamble, no explanation outside the JSON.

Required JSON fields:
- title: short incident title (max 80 chars)
- severity: "critical" | "high" | "medium" | "low"
- summary: 2-3 sentence plain-language description of what is happening
- suspected_root_cause: most likely technical cause
- affected_area: business area affected (e.g. "Withdrawals", "Authentication")
- suggested_owner: which team should own this (e.g. "Payments backend")
- recommended_next_steps: array of 3-5 concrete actions
- support_message: 1-sentence message for the support team to send users if needed"""


def generate_incident_summary(incident_data: dict) -> Optional[dict]:
    """
    Call Claude Haiku to generate a structured incident summary.
    Returns None if the API call fails (non-blocking).
    """
    try:
        prompt = f"""Analyze this production incident and return a JSON summary:

{json.dumps(incident_data, indent=2, default=str)}"""

        message = client.messages.create(
            model=settings.LLM_MODEL,
            max_tokens=1000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )

        raw_text = message.content[0].text.strip()

        # Strip any accidental markdown fences
        if raw_text.startswith("```"):
            raw_text = raw_text.split("```")[1]
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]

        summary = json.loads(raw_text.strip())
        logger.info(f"[LLM] Generated summary: {summary.get('title')}")
        return summary

    except anthropic.APIError as e:
        logger.error(f"[LLM] Anthropic API error: {e}")
        return None
    except json.JSONDecodeError as e:
        logger.error(f"[LLM] Failed to parse LLM response as JSON: {e}")
        return None
    except Exception as e:
        logger.error(f"[LLM] Unexpected error: {e}")
        return None


def build_incident_context(
    fingerprint: str,
    service: str,
    endpoint: str,
    http_status: Optional[int],
    affected_users: int,
    event_count: int,
    countries: list,
    severity: str,
    score: int,
    message: Optional[str],
    exception_type: Optional[str],
    first_seen_at: str,
    last_seen_at: str,
) -> dict:
    """Build the context dict to send to the LLM."""
    return {
        "fingerprint": fingerprint,
        "service": service,
        "endpoint": endpoint,
        "http_status": http_status,
        "affected_users": affected_users,
        "event_count": event_count,
        "countries": countries,
        "severity": severity,
        "score": score,
        # Redact PII before the error text leaves our boundary to Anthropic.
        "error_message": redact_pii(message),
        "exception_type": exception_type,
        "first_seen_at": first_seen_at,
        "last_seen_at": last_seen_at,
        "platform": "Airdrive fintech platform",
    }
