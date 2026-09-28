from computer_use.safety.redaction import redact_dict, redact_text


def test_redacts_ssn_pattern():
    out = redact_text("SSN on file: 123-45-6789")
    assert "123-45-6789" not in out
    assert "REDACTED" in out


def test_redacts_bearer_token():
    out = redact_text("Authorization: Bearer abc123XYZ_token")
    assert "abc123XYZ_token" not in out


def test_redacts_api_key_pattern():
    out = redact_text("key=sk-abcdefghijklmnop")
    assert "sk-abcdefghijklmnop" not in out


def test_leaves_ordinary_text_untouched():
    assert redact_text("member 10001 has a savings balance") == "member 10001 has a savings balance"


def test_redact_dict_masks_sensitive_keys_regardless_of_value_shape():
    out = redact_dict({"password": "hunter2", "member_id": "10001"})
    assert out["password"] == "[REDACTED]"
    assert out["member_id"] == "10001"


def test_redact_dict_recurses_into_nested_structures():
    out = redact_dict({"user": {"token": "sk-verysecrettoken12345", "name": "Jordan"}})
    assert out["user"]["token"] == "[REDACTED]"
    assert out["user"]["name"] == "Jordan"
