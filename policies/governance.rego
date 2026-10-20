package governance

# 治理團隊維護：規則依序判斷，第一個符合的 deny 原因即為結果
risk_rank := {"low": 1, "high": 2}

approver_role := "compliance_supervisor"

tool_key := sprintf("%s.%s", [input.tool.server, input.tool.name])

agent := data.agents[input.agent.id]

tool := data.tools[tool_key]

# 已有明確審批規則的高風險 tool；新增高風險 tool 而未寫規則時一律送審
reviewed_high_risk := {"account_ops.freeze_account", "account_ops.file_str"}

registered if agent.versions[input.agent.version].key_alias == input.agent.key_alias

deny_reason := "unregistered_agent_version" if {
	not registered
} else := "agent_suspended" if {
	agent.status != "active"
} else := "unknown_tool" if {
	not tool
} else := "risk_exceeds_agent_limit" if {
	risk_rank[tool.risk] > risk_rank[agent.max_risk]
} else := "role_required" if {
	input.tool.server == "account_ops"
	not "analyst" in input.user.roles
}

approval_reasons contains "sanctioned_counterparty" if {
	tool_key == "account_ops.freeze_account"
	lower(input.arguments.counterparty) in data.sanctions.addresses
}

approval_reasons contains "amount_over_threshold" if {
	tool_key == "account_ops.freeze_account"
	input.arguments.amount_usd >= tool.amount_threshold_usd
}

# 參數不存在時引用結果為 undefined，not 也會是 undefined；以 object.get 給預設值才能判斷缺漏
approval_reasons contains "amount_missing" if {
	tool_key == "account_ops.freeze_account"
	not is_number(object.get(input.arguments, "amount_usd", null))
}

approval_reasons contains "counterparty_missing" if {
	tool_key == "account_ops.freeze_account"
	not is_string(object.get(input.arguments, "counterparty", null))
}

approval_reasons contains "str_always_reviewed" if {
	tool_key == "account_ops.file_str"
}

approval_reasons contains "high_risk_unreviewed_tool" if {
	tool.risk == "high"
	not tool_key in reviewed_high_risk
}

decision := {
	"decision": "deny",
	"reasons": [deny_reason],
	"policy_version": data.meta.policy_version,
} if {
	deny_reason
} else := {
	"decision": "require_approval",
	"reasons": sort(approval_reasons),
	"approver_role": approver_role,
	"policy_version": data.meta.policy_version,
} if {
	count(approval_reasons) > 0
} else := {
	"decision": "allow",
	"reasons": [],
	"policy_version": data.meta.policy_version,
}
