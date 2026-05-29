"""
Earlybird — PII redaction.

For a fintech/KYC platform, raw error text can carry emails, account numbers,
card-like digits, and phone numbers. We scrub those before they leave our
boundary to the LLM (Anthropic) or get rendered into a Slack channel.

Conservative on purpose: we redact obvious PII patterns and leave short numbers
(HTTP statuses, counts) alone so incident context stays readable.
"""

import re
from typing import Optional

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# 6+ consecutive digits, optionally grouped by spaces/dashes (cards, accounts,
# phone numbers). 3-digit HTTP statuses and small counts are left untouched.
_LONG_NUMBER = re.compile(r"\b(?:\d[ -]?){6,}\d\b")
# Provider keys like sk-ant-..., and "Bearer/token/api_key/secret = <value>".
_API_KEY = re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9._-]{6,}")
_SECRET_KV = re.compile(
    r"(?i)\b(?:bearer|token|api[_-]?key|secret|authorization)\b\s*[:=]?\s*[A-Za-z0-9._-]{6,}"
)


def redact_pii(value: Optional[str]) -> Optional[str]:
    """Return `value` with emails, long digit sequences and secrets masked."""
    if not value:
        return value
    out = _EMAIL.sub("[EMAIL]", value)
    out = _SECRET_KV.sub("[REDACTED]", out)
    out = _API_KEY.sub("[REDACTED]", out)
    out = _LONG_NUMBER.sub("[NUMBER]", out)
    return out


def redact_summary(summary: Optional[dict]) -> Optional[dict]:
    """Scrub the free-text fields of an LLM summary before it hits Slack."""
    if not summary:
        return summary
    cleaned = dict(summary)
    for field in ("title", "summary", "suspected_root_cause", "support_message"):
        if isinstance(cleaned.get(field), str):
            cleaned[field] = redact_pii(cleaned[field])
    steps = cleaned.get("recommended_next_steps")
    if isinstance(steps, list):
        cleaned["recommended_next_steps"] = [
            redact_pii(s) if isinstance(s, str) else s for s in steps
        ]
    return cleaned
