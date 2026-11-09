package governance_llm_test

import data.governance

supported_entities := {"TW_ID", "TW_ARC", "PHONE", "EMAIL", "CREDIT_CARD", "BANK_ACCOUNT", "API_KEY"}

supported_exclusions := {"eth_address", "eth_tx_hash"}

valid_guardrails(g) if {
	is_array(g.pii.entities)
	every e in g.pii.entities {
		e in supported_entities
	}
	is_array(g.pii.exclude)
	every x in g.pii.exclude {
		x in supported_exclusions
	}
	g.injection.action in {"redact", "deny", "flag"}
	is_boolean(g.injection.system_notice)
}

invalid_guardrail_agents contains id if {
	some id, a in data.agents
	a.guardrails
	not valid_guardrails(a.guardrails)
}

kyt_v15 := {"id": "kyt-agent", "version": "1.5", "key_alias": "kyt-agent@1.5"}

test_unknown_agent_is_unregistered if {
	result := governance.llm with input as {"agent": {"id": "nobody", "version": "1.0", "key_alias": "nobody@1.0"}}
	result.agent_status == "unregistered"
}

test_unregistered_version_is_unregistered if {
	result := governance.llm with input as {"agent": {"id": "kyt-agent", "version": "9.9", "key_alias": "kyt-agent@9.9"}}
	result.agent_status == "unregistered"
}

test_key_alias_must_match_registered_version if {
	result := governance.llm with input as {"agent": {"id": "kyt-agent", "version": "1.5", "key_alias": "kyt-agent@1.4"}}
	result.agent_status == "unregistered"
}

test_missing_agent_is_unregistered if {
	result := governance.llm with input as {}
	result.agent_status == "unregistered"
}

test_kyt_agent_uses_its_config if {
	result := governance.llm with input as {"agent": kyt_v15}
	result.agent_status == "active"
	result.injection == {"action": "redact", "system_notice": false}
	result.pii.exclude == ["eth_address", "eth_tx_hash"]
	result.policy_version == data.meta.policy_version
}

registered_x(fields) := {"x": object.union(fields, {"versions": {"1.0": {"key_alias": "x@1.0"}}})}

x_v10 := {"id": "x", "version": "1.0", "key_alias": "x@1.0"}

test_agent_without_guardrails_gets_strict_defaults if {
	result := governance.llm with input as {"agent": x_v10} with data.agents as registered_x({"status": "active"})
	result.agent_status == "active"
	result.injection == {"action": "deny", "system_notice": true}
	count(result.pii.entities) == 7
	result.pii.exclude == []
}

test_non_active_agent_is_suspended if {
	result := governance.llm with input as {"agent": x_v10} with data.agents as registered_x({"status": "retired"})
	result.agent_status == "suspended"
}

test_unregistered_check_comes_before_suspension if {
	agents := {"x": {"status": "suspended", "versions": {}}}
	result := governance.llm with input as {"agent": x_v10} with data.agents as agents
	result.agent_status == "unregistered"
}

test_high_risk_tools_come_from_tools_data if {
	result := governance.llm with input as {"agent": kyt_v15}
	result.high_risk_tools == ["file_str", "freeze_account"]
}

test_incomplete_guardrails_make_result_undefined if {
	bad := registered_x({"status": "active", "guardrails": {"pii": {"entities": [], "exclude": []}}})
	not governance.llm with input as {"agent": x_v10} with data.agents as bad
}

test_guardrail_data_is_valid if {
	count(invalid_guardrail_agents) == 0
}

test_invalid_guardrail_data_is_detected if {
	bad := {"x": {"guardrails": {
		"pii": {"entities": ["NAME"], "exclude": []},
		"injection": {"action": "redact", "system_notice": false},
	}}}
	count(invalid_guardrail_agents) == 1 with data.agents as bad
}
