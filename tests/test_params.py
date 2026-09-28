import pytest

from computer_use import params


def test_make_placeholder_roundtrips_with_resolve():
    placeholder = params.make_placeholder("member_id")
    assert placeholder == "{{input.member_id}}"
    assert params.resolve(placeholder, {"member_id": "10002"}) == "10002"


def test_resolve_leaves_plain_text_untouched():
    assert params.resolve("no placeholders here", {}) == "no placeholders here"


def test_resolve_handles_multiple_placeholders_in_one_string():
    text = "/members/{{input.member_id}}/sub-account/{{input.account_type}}"
    out = params.resolve(text, {"member_id": "10001", "account_type": "savings"})
    assert out == "/members/10001/sub-account/savings"


def test_resolve_raises_on_missing_input():
    with pytest.raises(KeyError):
        params.resolve("{{input.member_id}}", {})


def test_referenced_params_extracts_names():
    text = "{{input.member_id}} and {{input.amount}}"
    assert params.referenced_params(text) == {"member_id", "amount"}


def test_resolve_none_returns_none():
    assert params.resolve(None, {"x": "1"}) is None
