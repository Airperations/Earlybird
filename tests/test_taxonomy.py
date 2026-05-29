"""Tests for the business taxonomy + language-agnostic keyword groups."""

from app import taxonomy


def test_derive_business_action_failed():
    assert taxonomy.derive_business_action("/api/v1/withdraw/confirm", 502, "GatewayTimeout") == "withdrawal_failed"
    assert taxonomy.derive_business_action("/deposit/create", 200, None) == "deposit"
    assert taxonomy.derive_business_action("/deposit/create", 400, None) == "deposit_failed"


def test_derive_business_action_unknown_endpoint_is_none():
    assert taxonomy.derive_business_action("/profile/avatar", 500, "Boom") is None


def test_base_action_strips_suffix():
    assert taxonomy.base_action("withdrawal_failed") == "withdrawal"
    assert taxonomy.base_action("deposit_not_credited") == "deposit"
    assert taxonomy.base_action("withdrawal") == "withdrawal"
    assert taxonomy.base_action(None) is None


def test_keyword_overlap_detects_spanish():
    r = taxonomy.detect_keyword_overlap("no me deja hacer un retiro", "withdrawal_failed")
    assert r["language"] == "es"
    assert "retiro" in r["overlap"]
    assert r["action_match"] is True
    assert "withdrawal" in r["groups"]


def test_keyword_overlap_detects_english():
    r = taxonomy.detect_keyword_overlap("my withdrawal failed", "withdrawal_failed")
    assert r["language"] == "en"
    assert "withdrawal" in r["overlap"]
    assert r["action_match"] is True


def test_keyword_overlap_detects_mixed():
    r = taxonomy.detect_keyword_overlap("deposit pendiente, no funciona", "deposit_failed")
    assert r["language"] == "mixed"
    assert "pending" in r["groups"] or "deposit" in r["groups"]


def test_keyword_overlap_none_for_unrelated():
    r = taxonomy.detect_keyword_overlap("how do I change my profile picture", "withdrawal_failed")
    assert r["language"] is None
    assert r["overlap"] == []
    assert r["action_match"] is False


def test_derive_business_action_new_actions():
    assert taxonomy.derive_business_action("/api/virtual-card/issue", 500, None) == "virtual_card_failed"
    assert taxonomy.derive_business_action("/virtual/account/create", 200, None) == "virtual_account"
    assert taxonomy.derive_business_action("/withdraw/direct", 200, None) == "direct_withdraw"
    assert taxonomy.derive_business_action("/auth/signup", 400, None) == "signup_failed"


def test_base_action_handles_multiword_actions():
    assert taxonomy.base_action("virtual_card_failed") == "virtual_card"
    assert taxonomy.base_action("direct_withdraw_failed") == "direct_withdraw"
    assert taxonomy.base_action("virtual_account") == "virtual_account"
    assert taxonomy.base_action("signup_failed") == "signup"


def test_keyword_overlap_matches_new_action_vocab():
    r = taxonomy.detect_keyword_overlap("no puedo crear mi tarjeta virtual", "virtual_card_failed")
    assert r["action_match"] is True
    assert "virtual_card" in r["groups"]


def test_build_normalized_keywords_includes_action_synonyms():
    kws = taxonomy.build_normalized_keywords(
        business_action="withdrawal_failed", provider="stripe",
        country="MX", endpoint="/withdraw/confirm",
    )
    assert "retiro" in kws and "withdraw" in kws
    assert "stripe" in kws
    assert "mx" in kws
