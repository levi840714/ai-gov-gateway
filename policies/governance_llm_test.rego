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

test_unregistered_agent_gets_strict_defaults if {
	result := governance.llm with input as {"agent": {"id": "nobody"}}
	result.agent_status == "unregistered"
	result.injection == {"action": "deny", "system_notice": true}
	count(result.pii.entities) == 7
	result.pii.exclude == []
}

test_missing_agent_id_is_unregistered if {
	result := governance.llm with input as {}
	result.agent_status == "unregistered"
	result.injection.action == "deny"
}

test_kyt_agent_uses_its_config if {
	result := governance.llm with input as {"agent": {"id": "kyt-agent"}}
	result.agent_status == "active"
	result.injection == {"action": "redact", "system_notice": false}
	result.pii.exclude == ["eth_address", "eth_tx_hash"]
	result.policy_version == data.meta.policy_version
}

test_agent_without_guardrails_gets_strict_defaults if {
	result := governance.llm with input as {"agent": {"id": "x"}} with data.agents as {"x": {"status": "active"}}
	result.agent_status == "active"
	result.injection.action == "deny"
}

test_non_active_agent_is_suspended if {
	result := governance.llm with input as {"agent": {"id": "x"}} with data.agents as {"x": {"status": "retired"}}
	result.agent_status == "suspended"
}

test_high_risk_tools_come_from_tools_data if {
	result := governance.llm with input as {"agent": {"id": "kyt-agent"}}
	result.high_risk_tools == ["file_str", "freeze_account"]
}

test_incomplete_guardrails_make_result_undefined if {
	bad := {"x": {"status": "active", "guardrails": {"pii": {"entities": [], "exclude": []}}}}
	not governance.llm with input as {"agent": {"id": "x"}} with data.agents as bad
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
