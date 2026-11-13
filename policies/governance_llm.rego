package governance

# LLM guardrail 的設定；已註冊但未設定 guardrails 的 agent 套用最嚴格的預設
# guardrails 欄位不完整時 llm 為 undefined，gateway 視為 policy_error 而拒絕
strict_guardrails := {
	"pii": {
		"entities": ["TW_ID", "TW_ARC", "PHONE", "EMAIL", "CREDIT_CARD", "BANK_ACCOUNT", "API_KEY"],
		"exclude": [],
	},
	"injection": {"action": "deny", "system_notice": true},
}

# registered、agent_active 定義在 governance.rego，與 tool 呼叫採同樣的判斷順序
llm_agent_status := "unregistered" if {
	not registered
} else := "suspended" if {
	not agent_active
} else := "active"

llm_guardrails := agent.guardrails if {
	is_object(agent.guardrails)
} else := strict_guardrails

# 已註冊但未設定 llm_limits 的 agent 套用最嚴格的額度；等級欄位不完整時 llm 為 undefined
strict_limits := {
	"tier": "strict",
	"role": "*",
	"budget_usd": {"day": 0.5, "month": 5},
	"rpm": 10,
	"tpm": 50000,
	"max_concurrent": 1,
	"max_input_chars": 50000,
	"max_output_tokens": 2048,
}

limit_counts := {"rpm", "tpm", "max_concurrent", "max_input_chars", "max_output_tokens"}

has_limits if object.get(agent, "llm_limits", null) != null

configured_tiers := agent.llm_limits if is_array(agent.llm_limits)

limits_invalid if {
	has_limits
	not configured_tiers
}

limits_invalid if {
	some tier in configured_tiers
	not valid_tier(tier)
}

valid_tier(tier) if {
	is_string(tier.tier)
	is_string(tier.role)
	valid_budget(tier.budget_usd)
	every name in limit_counts {
		positive_int(tier[name])
	}
}

valid_budget(budget) if {
	is_object(budget)
	count(budget) > 0
	every period, amount in budget {
		period in {"day", "month"}
		is_number(amount)
		amount > 0
	}
}

positive_int(value) if {
	is_number(value)
	value == floor(value)
	value > 0
}

tier_applies(tier) if tier.role == "*"

tier_applies(tier) if {
	is_array(input.user.roles)
	tier.role in input.user.roles
}

matching_tiers := [tier | some tier in configured_tiers; tier_applies(tier)]

llm_limits := strict_limits if {
	not has_limits
} else := matching_tiers[0] if {
	count(matching_tiers) > 0
} else := null

high_risk_tools contains name if {
	some key, tool_data in data.tools
	tool_data.risk == "high"
	parts := split(key, ".")
	name := parts[count(parts) - 1]
}

llm := {
	"agent_status": llm_agent_status,
	"pii": llm_guardrails.pii,
	"injection": llm_guardrails.injection,
	"high_risk_tools": sort(high_risk_tools),
	"limits": llm_limits,
	"policy_version": data.meta.policy_version,
} if not limits_invalid
