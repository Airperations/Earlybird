from app.redaction import redact_pii, redact_summary


def test_redacts_email():
    assert redact_pii("contact user_demo@gmail.com about it") == "contact [EMAIL] about it"


def test_redacts_long_numbers_but_not_http_status():
    assert redact_pii("card 4111111111111111 failed") == "card [NUMBER] failed"
    # 3-digit HTTP statuses and small counts are preserved.
    assert redact_pii("HTTP 502 on 3 requests") == "HTTP 502 on 3 requests"


def test_redacts_secrets():
    out = redact_pii("Authorization: Bearer sk-ant-abc123")
    assert "sk-ant" not in out
    assert "[REDACTED]" in out


def test_redact_none_and_empty():
    assert redact_pii(None) is None
    assert redact_pii("") == ""


def test_redact_summary_scrubs_text_fields():
    summary = {
        "title": "Error for jane.doe@airtm.io",
        "summary": "User 998877665544 affected",
        "severity": "critical",
        "recommended_next_steps": ["call 998877665544", "check logs"],
    }
    out = redact_summary(summary)
    assert "jane.doe@airtm.io" not in out["title"]
    assert "[NUMBER]" in out["summary"]
    assert out["recommended_next_steps"][0] == "call [NUMBER]"
    assert out["severity"] == "critical"  # non-text field untouched


def test_redact_summary_none():
    assert redact_summary(None) is None
