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
	"policy_version": data.meta.policy_version,
}
