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

# --- fail-closed：欄位缺漏、型別錯誤、資料錯誤 ---

test_missing_user_denied if {
	input_doc := object.remove(freeze(5000, clean), {"user"})
	governance.decision.reasons == ["role_required"] with input as input_doc
}

test_roles_not_a_list_denied if {
	input_doc := object.union(freeze(5000, clean), {"user": {"sub": "s", "username": "u", "roles": "analyst"}})
	governance.decision.reasons == ["role_required"] with input as input_doc
}

test_missing_arguments_requires_approval if {
	input_doc := object.remove(freeze(5000, clean), {"arguments"})
	result := governance.decision with input as input_doc
	result.reasons == ["amount_missing", "counterparty_missing"]
}

test_null_arguments_requires_approval if {
	input_doc := object.union(freeze(5000, clean), {"arguments": null})
	result := governance.decision with input as input_doc
	result.reasons == ["amount_missing", "counterparty_missing"]
}

test_string_amount_requires_approval if {
	governance.decision.reasons == ["amount_missing"] with input as freeze("25000", clean)
}

test_non_positive_amount_requires_approval if {
	governance.decision.reasons == ["amount_invalid"] with input as freeze(-1, clean)
	governance.decision.reasons == ["amount_invalid"] with input as freeze(0, clean)
}

test_threshold_boundary if {
	governance.decision.decision == "allow" with input as freeze(9999.99, clean)
	governance.decision.reasons == ["amount_over_threshold"] with input as freeze(10000, clean)
}

test_padded_sanctioned_address_still_matches if {
	result := governance.decision with input as freeze(10, concat("", ["  ", sanctioned, "\n"]))
	result.reasons == ["sanctioned_counterparty"]
}

test_malformed_counterparty_requires_approval if {
	governance.decision.reasons == ["counterparty_invalid"] with input as freeze(10, "8589427373d6d84e98730d7795d8f6f8731fda16")
	governance.decision.reasons == ["counterparty_invalid"] with input as freeze(10, "")
}

test_unknown_risk_value_denied if {
	tools := object.union(data.tools, {"account_ops.close_account": {"risk": "critical"}})
	result := governance.decision with input as call("close_account", {}) with data.tools as tools
	result.reasons == ["risk_exceeds_agent_limit"]
}

test_agent_without_status_denied if {
	agents := {"kyt-agent": object.remove(data.agents["kyt-agent"], {"status"})}
	result := governance.decision with input as freeze(5000, clean) with data.agents as agents
	result.reasons == ["agent_suspended"]
}

test_missing_threshold_requires_approval if {
	# object.union 會遞迴合併，要先移除整個 entry 才能拿掉門檻
	base := object.remove(data.tools, {"account_ops.freeze_account"})
	tools := object.union(base, {"account_ops.freeze_account": {"risk": "high"}})
	result := governance.decision with input as freeze(5000, clean) with data.tools as tools
	result.reasons == ["threshold_invalid"]
}

test_empty_sanctions_list_requires_approval if {
	result := governance.decision with input as freeze(10, clean) with data.sanctions as {"addresses": []}
	result.reasons == ["sanctions_list_unavailable"]
}

test_deny_precedence if {
	suspended := object.union(data.agents, {"kyt-agent": {"status": "suspended"}})
	input_doc := object.union(freeze(5000, clean), {"user": bob})
	result := governance.decision with input as input_doc with data.agents as suspended
	result.reasons == ["agent_suspended"]
}

test_file_str_requires_analyst if {
	input_doc := object.union(call("file_str", {"account_id": "a", "summary": "s"}), {"user": bob})
	governance.decision.reasons == ["role_required"] with input as input_doc
}

test_data_integrity if {
	every _, entry in data.tools {
		entry.risk in object.keys(governance.risk_rank)
	}
	every _, entry in data.agents {
		entry.status in {"active", "suspended"}
		entry.max_risk in object.keys(governance.risk_rank)
	}
	count(data.sanctions.addresses) > 0
	every address in data.sanctions.addresses {
		regex.match(`^0x[0-9a-f]{40}$`, address)
	}
	is_number(data.tools["account_ops.freeze_account"].amount_threshold_usd)
}
