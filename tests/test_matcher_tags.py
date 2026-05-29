from app.freshdesk.matcher import normalize_tags


def test_normalize_tags_handles_real_freshdesk_strings():
    # Real Freshdesk API v2 returns tags as plain strings.
    assert normalize_tags(["MX", "withdrawal"]) == ["MX", "WITHDRAWAL"]


def test_normalize_tags_handles_demo_dicts():
    # The demo / webhook automations send dicts.
    assert normalize_tags([{"name": "mx"}, {"name": "error"}]) == ["MX", "ERROR"]


def test_normalize_tags_handles_none_and_empty():
    assert normalize_tags(None) == []
    assert normalize_tags([]) == []
    assert normalize_tags([{"name": ""}, ""]) == []
