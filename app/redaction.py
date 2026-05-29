"""
Earlybird — PII redaction.

For a fintech/KYC platform, raw error text can carry emails, account numbers,
card-like digits, and phone numbers. We scrub those before they leave our
boundary to the LLM (Anthropic) or get rendered into a Slack channel.

Conservative on purpose: we redact obvious PII patterns and leave short numbers
(HTTP statuses, counts) alone so incident context stays readable.
"""

import hashlib
import re
from typing import Optional

from app.config import settings

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


# ── Identifier hashing ────────────────────────────────────────────────────────
# Raw user/account identifiers are PII on a KYC platform, so they must never be
# persisted, logged, or sent to Slack/the LLM. We instead store a salted,
# one-way hash: distinct users still map to distinct hashes (so "affected users"
# stays accurate and overlap detection works), but the original id is
# unrecoverable from what we keep.
_USER_HASH_PREFIX = "u_"

# Key names whose VALUES are an identifier to hash (in webhook payloads).
_ID_KEYS = {
    "user_id", "userid", "uid", "customer_id", "customerid",
    "account_id", "accountid", "account_number", "accountnumber",
    "card_number", "cardnumber", "pan",
}
# Key names whose values are an email → masked, not hashed (emails aren't matched on).
_EMAIL_KEYS = {"email", "requester_email", "user_email", "contact_email"}
# Key names that hold secrets/phones → fully redacted.
_SECRET_KEYS = {
    "token", "access_token", "refresh_token", "api_key", "apikey",
    "secret", "authorization", "password", "phone", "phone_number",
    "phonenumber", "msisdn",
}
# Parent keys that mark an identity object; a nested "id" inside is hashed.
_IDENTITY_PARENTS = {"user", "requester", "customer", "sender", "recipient", "payer", "payee"}


def hash_identifier(value) -> Optional[str]:
    """
    Salted one-way hash of a user/account identifier. Returns None for empty
    input so 'no user' stays distinguishable from 'some user'. Deterministic for
    a given USER_HASH_SALT, so the same user always hashes to the same value.
    """
    if value in (None, ""):
        return None
    salted = f"{settings.USER_HASH_SALT}:{value}".encode()
    return _USER_HASH_PREFIX + hashlib.sha256(salted).hexdigest()[:16]


def redact_payload(obj, _in_identity: bool = False):
    """
    Recursively scrub a raw webhook payload before it is persisted (raw_events /
    freshdesk raw_payload). Identifiers are hashed, emails masked, secrets/phones
    redacted, and every free-text string passed through redact_pii. Non-sensitive
    structure is preserved so the payload is still useful for forensics.
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            kl = str(k).lower()
            identity_parent = kl in _IDENTITY_PARENTS
            if kl in _EMAIL_KEYS:
                out[k] = redact_payload(v, identity_parent) if isinstance(v, (dict, list)) else "[EMAIL]"
            elif kl in _ID_KEYS or (_in_identity and kl == "id"):
                out[k] = redact_payload(v, identity_parent) if isinstance(v, (dict, list)) else hash_identifier(v)
            elif kl in _SECRET_KEYS:
                out[k] = redact_payload(v, identity_parent) if isinstance(v, (dict, list)) else "[REDACTED]"
            else:
                out[k] = redact_payload(v, identity_parent or _in_identity)
        return out
    if isinstance(obj, list):
        return [redact_payload(x, _in_identity) for x in obj]
    if isinstance(obj, str):
        return redact_pii(obj)
    return obj


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
