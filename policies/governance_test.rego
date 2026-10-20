package governance_test

import data.governance

alice := {"sub": "alice-sub", "username": "alice", "roles": ["analyst"]}

bob := {"sub": "bob-sub", "username": "bob", "roles": ["compliance_supervisor"]}

agent_14 := {"id": "kyt-agent", "version": "1.4", "key_alias": "kyt-agent@1.4"}

call(tool, arguments) := {
	"agent": agent_14,
	"user": alice,
	"tool": {"server": "account_ops", "name": tool},
	"arguments": arguments,
}

freeze(amount, counterparty) := call("freeze_account", {
	"account_id": "acct-001",
	"amount_usd": amount,
	"counterparty": counterparty,
	"reason": "test",
})

clean := "0x1111111111111111111111111111111111111111"

sanctioned := "0x8589427373D6D84E98730D7795D8F6F8731FDA16"

test_watchlist_allowed if {
	result := governance.decision with input as call("add_to_watchlist", {"account_id": "a", "reason": "r"})
	result.decision == "allow"
	result.policy_version == "2026-10-20.1"
}

test_small_freeze_allowed if {
	governance.decision.decision == "allow" with input as freeze(5000, clean)
}

test_large_freeze_requires_approval if {
	result := governance.decision with input as freeze(25000, clean)
	result.decision == "require_approval"
	result.reasons == ["amount_over_threshold"]
	result.approver_role == "compliance_supervisor"
}

test_sanctioned_counterparty_requires_approval_case_insensitive if {
	result := governance.decision with input as freeze(10, sanctioned)
	result.reasons == ["sanctioned_counterparty"]
}

test_sanctioned_and_large_lists_both_reasons if {
	result := governance.decision with input as freeze(25000, sanctioned)
	result.reasons == ["amount_over_threshold", "sanctioned_counterparty"]
}

test_missing_amount_requires_approval if {
	input_doc := call("freeze_account", {"account_id": "a", "counterparty": clean, "reason": "r"})
	result := governance.decision with input as input_doc
	result.reasons == ["amount_missing"]
}

test_missing_counterparty_requires_approval if {
	input_doc := call("freeze_account", {"account_id": "a", "amount_usd": 10, "reason": "r"})
	result := governance.decision with input as input_doc
	result.reasons == ["counterparty_missing"]
}

test_str_always_reviewed if {
	result := governance.decision with input as call("file_str", {"account_id": "a", "summary": "s"})
	result.reasons == ["str_always_reviewed"]
}

test_unregistered_version_denied if {
	input_doc := object.union(freeze(5000, clean), {"agent": {"id": "kyt-agent", "version": "1.5", "key_alias": "kyt-agent@1.5"}})
	result := governance.decision with input as input_doc
	result.decision == "deny"
	result.reasons == ["unregistered_agent_version"]
}

test_unknown_agent_denied if {
	input_doc := object.union(freeze(5000, clean), {"agent": {"id": "other", "version": "1", "key_alias": "other@1"}})
	governance.decision.reasons == ["unregistered_agent_version"] with input as input_doc
}

test_suspended_agent_denied if {
	suspended := object.union(data.agents, {"kyt-agent": {"status": "suspended"}})
	result := governance.decision with input as freeze(25000, clean) with data.agents as suspended
	result.reasons == ["agent_suspended"]
}

test_unknown_tool_denied if {
	governance.decision.reasons == ["unknown_tool"] with input as call("delete_account", {})
}

test_risk_exceeds_agent_limit if {
	limited := object.union(data.agents, {"kyt-agent": {"max_risk": "low"}})
	result := governance.decision with input as freeze(5000, clean) with data.agents as limited
	result.reasons == ["risk_exceeds_agent_limit"]
}

test_role_required_for_account_ops if {
	input_doc := object.union(freeze(5000, clean), {"user": bob})
	governance.decision.reasons == ["role_required"] with input as input_doc
}

test_probe_allowed_without_analyst_role if {
	input_doc := {"agent": agent_14, "user": bob, "tool": {"server": "probe", "name": "whoami"}, "arguments": {}}
	governance.decision.decision == "allow" with input as input_doc
}

test_new_high_risk_tool_defaults_to_review if {
	tools := object.union(data.tools, {"account_ops.close_account": {"risk": "high"}})
	result := governance.decision with input as call("close_account", {}) with data.tools as tools
	result.reasons == ["high_risk_unreviewed_tool"]
}

test_policy_version_present if {
	is_string(data.meta.policy_version)
	data.meta.policy_version != ""
}
