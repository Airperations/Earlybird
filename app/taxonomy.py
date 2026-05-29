"""
Earlybird — Business taxonomy & multilingual keyword groups.

This is the single source of truth that lets an incident answer, *without reading
an LLM summary*:

    What broke?  (business_action)        e.g. "withdrawal_failed"
    Where?       (service / endpoint)
    Who/where?   (primary_country / provider / payment_method / platform)

and that lets the Freshdesk matcher recognise a user's complaint in **any
supported language** ("no me deja retirar", "withdrawal failed", "depósito
pendiente") via *language-agnostic* keyword groups.

Design notes:
  • Keyword groups are organised by MEANING, not by language. Each group carries
    both English and Spanish surface forms. The matcher reports a generic
    `keyword_match` signal plus the *detected* `keyword_language` (en/es/mixed) —
    never a language-specific match label.
  • Everything here is pure (no I/O, no settings) so it is trivially testable and
    shared by both the normalizer and the matcher.
"""

import re
from typing import Dict, List, Optional


# ── Multilingual keyword groups (by meaning) ──────────────────────────────────
# Each group maps a language tag → surface forms. Multi-word forms are matched as
# substrings, so "cash out" / "no me deja" work even though they contain spaces.
KEYWORD_GROUPS: Dict[str, Dict[str, List[str]]] = {
    "withdrawal": {
        "en": ["withdraw", "withdrawal", "cash out", "payout", "take out money"],
        "es": ["retiro", "retirar", "sacar dinero", "sacar plata", "retir"],
    },
    "deposit": {
        "en": ["deposit", "top up", "add funds", "add money", "not credited", "credit"],
        "es": ["deposito", "depósito", "depositar", "recarga", "abono", "no se acredito", "no se acreditó", "no acreditado"],
    },
    "transfer": {
        "en": ["transfer", "send money", "send funds"],
        "es": ["transferencia", "transferir", "envio", "envío", "enviar dinero"],
    },
    "p2p": {
        "en": ["p2p", "peer to peer"],
        "es": ["p2p"],
    },
    "balance": {
        "en": ["balance", "wrong balance", "missing funds"],
        "es": ["saldo", "saldo incorrecto", "fondos", "mi saldo esta mal", "mi saldo está mal"],
    },
    "login": {
        "en": ["login", "log in", "sign in", "cannot access", "access"],
        "es": ["iniciar sesion", "iniciar sesión", "acceso", "no puedo entrar", "entrar"],
    },
    "payment": {
        "en": ["payment", "charge", "card declined"],
        "es": ["pago", "pagar", "cobro", "tarjeta rechazada"],
    },
    "signup": {
        "en": ["sign up", "signup", "register", "registration", "create account",
               "onboarding", "kyc", "verify identity", "identity verification"],
        "es": ["registro", "registrarse", "crear cuenta", "verificación", "verificacion",
               "verificar identidad", "onboarding", "alta de cuenta"],
    },
    "virtual_account": {
        "en": ["virtual account", "account number", "banking details", "clabe", "iban"],
        "es": ["cuenta virtual", "número de cuenta", "numero de cuenta", "datos bancarios", "clabe"],
    },
    "virtual_card": {
        "en": ["virtual card", "card issuance", "issue card", "prepaid card", "create card"],
        "es": ["tarjeta virtual", "emitir tarjeta", "emisión de tarjeta", "emision de tarjeta",
               "tarjeta prepago", "crear tarjeta"],
    },
    "direct_withdraw": {
        "en": ["direct withdraw", "direct withdrawal", "instant withdraw", "instant withdrawal"],
        "es": ["retiro directo", "retiro inmediato"],
    },
    # Modifier groups — describe the *failure mode*, combine with an action above.
    "pending": {
        "en": ["pending", "stuck", "still processing", "not received yet"],
        "es": ["pendiente", "cargando", "trabado", "se quedo pendiente", "se quedó pendiente", "no me llega"],
    },
    "failed": {
        "en": ["failed", "error", "unable", "cannot", "can't", "not working", "doesn't work"],
        "es": ["fallo", "falló", "falla", "no funciona", "no me deja", "no puedo", "problema"],
    },
}

# Groups that name a business action (vs. a failure modifier).
ACTION_GROUPS = (
    "withdrawal", "direct_withdraw", "deposit", "transfer", "p2p", "balance",
    "login", "payment", "signup", "virtual_account", "virtual_card",
)
# Money-flow actions where even a small absolute sample is meaningful (lower the
# baseline sample-size bar so a handful of failures can still trip).
CRITICAL_ACTIONS = frozenset({
    "withdrawal", "direct_withdraw", "deposit", "transfer", "p2p", "payment",
    "virtual_account", "virtual_card",
})
# Groups that describe a failure mode.
MODIFIER_GROUPS = ("pending", "failed")

# Flat set of every financial/platform keyword — used as the cheap "is this even
# a money/platform ticket" gate. Replaces the old hardcoded FINANCIAL_KEYWORDS.
ALL_KEYWORDS = {
    kw for group in KEYWORD_GROUPS.values() for forms in group.values() for kw in forms
}


def base_action(business_action: Optional[str]) -> Optional[str]:
    """
    Strip the failure-mode suffix from a business action, handling multi-word
    action names (e.g. virtual_card) correctly.

        "withdrawal_failed"      → "withdrawal"
        "virtual_card_failed"    → "virtual_card"
        "direct_withdraw_failed" → "direct_withdraw"
        "deposit_not_credited"   → "deposit"
        "withdrawal"             → "withdrawal"
    """
    if not business_action:
        return None
    ba = business_action.lower()
    if ba in ACTION_GROUPS:
        return ba
    # Longest matching action prefix wins (so virtual_card beats a stray match).
    for action in sorted(ACTION_GROUPS, key=len, reverse=True):
        if ba == action or ba.startswith(action + "_"):
            return action
    return ba.split("_", 1)[0]


def derive_business_action(
    endpoint: Optional[str],
    http_status: Optional[int] = None,
    exception_type: Optional[str] = None,
) -> Optional[str]:
    """
    Map a technical endpoint + outcome to a normalized business action like
    `withdrawal_failed` or `virtual_card_failed`. Returns None when the endpoint
    isn't a known money/platform flow, so non-business errors don't get a
    misleading label. Most-specific endpoints are matched first.
    """
    ep = (endpoint or "").lower()
    base = None
    if "virtual" in ep and "card" in ep:
        base = "virtual_card"
    elif "virtual" in ep and ("account" in ep or "wallet" in ep):
        base = "virtual_account"
    elif "direct" in ep and "withdraw" in ep:
        base = "direct_withdraw"
    elif "withdraw" in ep:
        base = "withdrawal"
    elif "deposit" in ep:
        base = "deposit"
    elif "transfer" in ep:
        base = "transfer"
    elif "p2p" in ep:
        base = "p2p"
    elif "balance" in ep:
        base = "balance"
    elif "signup" in ep or "sign-up" in ep or "register" in ep or "onboard" in ep or "kyc" in ep:
        base = "signup"
    elif "login" in ep or "auth" in ep or "session" in ep:
        base = "login"
    elif "pay" in ep or "charge" in ep:
        base = "payment"
    if base is None:
        return None

    failed = bool(exception_type) or (http_status is not None and http_status >= 400)
    return f"{base}_failed" if failed else base


def build_normalized_keywords(
    *,
    business_action: Optional[str] = None,
    provider: Optional[str] = None,
    payment_method: Optional[str] = None,
    country: Optional[str] = None,
    exception_type: Optional[str] = None,
    endpoint: Optional[str] = None,
) -> List[str]:
    """
    Pre-compute the searchable keyword footprint of an incident so the matcher
    (and a human auditor) can see, at a glance, what vocabulary a related ticket
    would use. Stored on the incident as `normalized_keywords`.
    """
    kws = set()
    base = base_action(business_action)
    if base and base in KEYWORD_GROUPS:
        for forms in KEYWORD_GROUPS[base].values():
            kws.update(forms)
    if business_action:
        kws.add(business_action.lower())
    for v in (provider, payment_method, country, exception_type):
        if v:
            kws.add(str(v).lower())
    if endpoint:
        for tok in re.split(r"[^a-z0-9]+", endpoint.lower()):
            if len(tok) > 2 and tok != "id":
                kws.add(tok)
    return sorted(kws)


def detect_keyword_overlap(text: str, business_action: Optional[str] = None) -> dict:
    """
    Scan `text` for keyword-group hits and report a *language-agnostic* result:

        {
          "overlap":   ["retiro", "falló"],   # the surface forms that hit
          "groups":    ["failed", "withdrawal"],
          "language":  "es" | "en" | "mixed" | None,
          "action_match": True,               # ticket mentions the incident's action
        }

    `business_action` (the incident's) is only used to set `action_match` — the
    scan itself is over every group, so the matcher can also surface failure-mode
    vocabulary (pending / failed) regardless of the incident's primary action.
    """
    text_l = (text or "").lower()
    overlap_en, overlap_es, groups = set(), set(), set()

    for group, forms in KEYWORD_GROUPS.items():
        for kw in forms.get("en", []):
            if kw in text_l:
                overlap_en.add(kw)
                groups.add(group)
        for kw in forms.get("es", []):
            if kw in text_l:
                overlap_es.add(kw)
                groups.add(group)

    if overlap_en and overlap_es:
        language = "mixed"
    elif overlap_es:
        language = "es"
    elif overlap_en:
        language = "en"
    else:
        language = None

    base = base_action(business_action)
    return {
        "overlap": sorted(overlap_en | overlap_es),
        "groups": sorted(groups),
        "language": language,
        "action_match": bool(base and base in groups),
    }
