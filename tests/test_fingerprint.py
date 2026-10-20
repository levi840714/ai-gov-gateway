import json
from datetime import UTC, datetime

import pytest

from ai_gov_gateway.engine.fingerprint import call_fingerprint, canonical_json, parse_agent_key
from ai_gov_gateway.engine.models import AgentRef, Outcome

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
