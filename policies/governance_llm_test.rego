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

tier(name, role) := {
	"tier": name,
	"role": role,
	"budget_usd": {"day": 1, "month": 10},
	"rpm": 10,
	"tpm": 1000,
	"max_concurrent": 2,
	"max_input_chars": 1000,
	"max_output_tokens": 100,
}

limits_x(tiers) := registered_x({"status": "active", "llm_limits": tiers})

carol := {"roles": ["analyst", "compliance_supervisor"]}

alice := {"roles": ["analyst"]}

three_tiers := [tier("probe", "nobody"), tier("supervisor", "compliance_supervisor"), tier("default", "*")]

test_limits_first_matching_tier if {
	result := governance.llm with input as {"agent": x_v10, "user": carol} with data.agents as limits_x(three_tiers)
	result.limits.tier == "supervisor"
}

test_limits_wildcard_tier if {
	result := governance.llm with input as {"agent": x_v10, "user": alice} with data.agents as limits_x(three_tiers)
	result.limits.tier == "default"
}

test_limits_null_without_matching_tier if {
	agents := limits_x([tier("supervisor", "compliance_supervisor")])
	result := governance.llm with input as {"agent": x_v10, "user": alice} with data.agents as agents
	result.limits == null
}

test_limits_strict_default_when_unconfigured if {
	result := governance.llm with input as {"agent": x_v10, "user": alice} with data.agents as registered_x({"status": "active"})
	result.limits == governance.strict_limits
	result.limits.tier == "strict"
}

test_unregistered_agent_still_has_llm_with_strict_limits if {
	result := governance.llm with input as {"agent": {"id": "nobody", "version": "1.0", "key_alias": "nobody@1.0"}}
	result.agent_status == "unregistered"
	result.limits.tier == "strict"
}

test_llm_undefined_when_tier_invalid if {
	base := tier("t", "*")
	bad_tiers := [
		object.remove(base, ["max_concurrent"]),
		object.union(base, {"budget_usd": 5}),
		json.patch(base, [{"op": "replace", "path": "/budget_usd", "value": {}}]),
		json.patch(base, [{"op": "replace", "path": "/budget_usd", "value": {"week": 1}}]),
		object.union(base, {"budget_usd": {"day": 1, "week": 1}}),
		object.union(base, {"budget_usd": {"day": -1}}),
		object.union(base, {"rpm": -1}),
		object.union(base, {"rpm": 1.5}),
		object.union(base, {"tpm": "10"}),
		"default",
	]
	every bad in bad_tiers {
		not governance.llm with input as {"agent": x_v10, "user": alice} with data.agents as limits_x([bad])
	}
}

test_llm_undefined_when_one_of_many_tiers_invalid if {
	tiers := [tier("ok", "*"), object.union(tier("bad", "x"), {"rpm": 0})]
	not governance.llm with input as {"agent": x_v10, "user": alice} with data.agents as limits_x(tiers)
}

test_llm_undefined_when_limits_not_array if {
	not governance.llm with input as {"agent": x_v10, "user": alice} with data.agents as limits_x(tier("t", "*"))
}

test_roles_not_array_matches_only_wildcard if {
	bad_roles := {"roles": "compliance_supervisor"}
	result := governance.llm with input as {"agent": x_v10, "user": bad_roles} with data.agents as limits_x(three_tiers)
	result.limits.tier == "default"
	no_wildcard := limits_x([tier("supervisor", "compliance_supervisor")])
	none := governance.llm with input as {"agent": x_v10, "user": bad_roles} with data.agents as no_wildcard
	none.limits == null
}

test_missing_user_matches_only_wildcard if {
	result := governance.llm with input as {"agent": x_v10} with data.agents as limits_x(three_tiers)
	result.limits.tier == "default"
}

test_kyt_agent_limit_tiers if {
	supervisor := governance.llm with input as {"agent": kyt_v15, "user": carol}
	supervisor.limits.tier == "supervisor"
	probe := governance.llm with input as {"agent": kyt_v15, "user": {"roles": ["llm_quota_probe"]}}
	probe.limits.tier == "quota_probe"
	fallback := governance.llm with input as {"agent": kyt_v15, "user": alice}
	fallback.limits.tier == "default"
}
