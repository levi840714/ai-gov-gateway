import json
from datetime import UTC, datetime

import pytest

from ai_gov_gateway.engine.fingerprint import call_fingerprint, canonical_json, parse_agent_key
from ai_gov_gateway.engine.models import AgentRef, Outcome, ToolCall

AGENT = AgentRef(id="kyt-agent", version="1.4", key_alias="kyt-agent@1.4")


def test_canonical_json_sorts_keys_without_spaces():
    assert canonical_json({"b": 1, "a": {"d": 2, "c": 3}}) == '{"a":{"c":3,"d":2},"b":1}'


def test_fingerprint_ignores_argument_order():
    first = call_fingerprint(AGENT, "alice", "account_ops", "freeze_account", {"a": 1, "b": 2})
    second = call_fingerprint(AGENT, "alice", "account_ops", "freeze_account", {"b": 2, "a": 1})
    assert first == second


def test_fingerprint_binds_arguments_user_and_version():
    base = call_fingerprint(AGENT, "alice", "s", "t", {"amount_usd": 1})
    assert base != call_fingerprint(AGENT, "alice", "s", "t", {"amount_usd": 2})
    assert base != call_fingerprint(AGENT, "bob", "s", "t", {"amount_usd": 1})
    newer = AgentRef(id="kyt-agent", version="1.5", key_alias="kyt-agent@1.5")
    assert base != call_fingerprint(newer, "alice", "s", "t", {"amount_usd": 1})
    assert base != call_fingerprint(AGENT, "alice", "other", "t", {"amount_usd": 1})
    assert base != call_fingerprint(AGENT, "alice", "s", "other", {"amount_usd": 1})


def test_canonical_json_handles_nested_lists_non_ascii_and_none():
    value = {"z": [1, {"b": None, "a": "臺北"}]}
    assert canonical_json(value) == '{"z":[1,{"a":"臺北","b":null}]}'


def test_canonical_json_rejects_non_json_values():
    with pytest.raises(TypeError):
        canonical_json({"ids": {1, 2}})
    with pytest.raises(ValueError):
        canonical_json({"amount_usd": float("nan")})


def test_fingerprint_golden_vector():
    # 指紋會持久化，canonical 形式一旦改變，既有核准就失效
    fingerprint = call_fingerprint(
        AGENT,
        "alice-sub",
        "account_ops",
        "freeze_account",
        {"account_id": "acct-001", "amount_usd": 25000},
    )
    assert fingerprint == "47abfcf5f77033d942b96ad7bf1a42255cf97584d7bbc60ec3fa6afa23fa3808"


def test_fingerprint_distinguishes_int_from_float():
    # 刻意採 fail-safe：型別被改寫的值需要重新核准
    as_int = call_fingerprint(AGENT, "alice", "s", "t", {"amount_usd": 100})
    assert as_int != call_fingerprint(AGENT, "alice", "s", "t", {"amount_usd": 100.0})


@pytest.mark.parametrize("alias", [None, "", "kyt-agent", "kyt-agent@", "@1.4", "a@b@c"])
def test_parse_agent_key_rejects_malformed(alias):
    assert parse_agent_key(alias) is None


def test_parse_agent_key():
    assert parse_agent_key("kyt-agent@1.4") == AGENT


def test_outcome_messages_are_json():
    expires = datetime(2026, 10, 21, 10, tzinfo=UTC)
    pending = Outcome.pending("apr_1", expires, ["amount_over_threshold"])
    assert json.loads(pending.to_message()) == {
        "governance": "pending_approval",
        "reasons": ["amount_over_threshold"],
        "approval_id": "apr_1",
        "expires_at": "2026-10-21T10:00:00+00:00",
    }
    denied = Outcome.denied(["role_required"])
    assert json.loads(denied.to_message()) == {"governance": "denied", "reasons": ["role_required"]}


def test_tool_call_repr_hides_user_token():
    call = ToolCall(key_alias="kyt-agent@1.4", user_token="eyJ.secret.token", server="s", tool="t")
    assert "secret.token" not in repr(call)
