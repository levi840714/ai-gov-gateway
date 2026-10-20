package governance

# 治理團隊維護：規則依序判斷，第一個符合的 deny 原因即為結果
# 守門條件一律寫成「正面的 helper + not」：欄位缺漏或資料錯誤時 helper 為 undefined，not 才會成立
risk_rank := {"low": 1, "high": 2}

approver_role := "compliance_supervisor"

tool_key := sprintf("%s.%s", [input.tool.server, input.tool.name])

agent := data.agents[input.agent.id]

tool := data.tools[tool_key]

# 已有明確審批規則的高風險 tool；新增高風險 tool 而未寫規則時一律送審
reviewed_high_risk := {"account_ops.freeze_account", "account_ops.file_str"}

args := input.arguments if {
	is_object(input.arguments)
} else := {}

registered if agent.versions[input.agent.version].key_alias == input.agent.key_alias

agent_active if agent.status == "active"

within_risk_limit if risk_rank[tool.risk] <= risk_rank[agent.max_risk]

is_analyst if {
	is_array(input.user.roles)
	"analyst" in input.user.roles
}

deny_reason := "unregistered_agent_version" if {
	not registered
} else := "agent_suspended" if {
	not agent_active
} else := "unknown_tool" if {
	not tool
} else := "risk_exceeds_agent_limit" if {
	not within_risk_limit
} else := "role_required" if {
	input.tool.server == "account_ops"
	not is_analyst
}

freezing if tool_key == "account_ops.freeze_account"

amount := args.amount_usd if is_number(args.amount_usd)

threshold := tool.amount_threshold_usd if is_number(tool.amount_threshold_usd)

# 鏈上地址比對前先去空白、轉小寫，並要求標準格式，避免以空白或大小寫繞過制裁比對
counterparty := lower(trim_space(args.counterparty)) if is_string(args.counterparty)

valid_counterparty if regex.match(`^0x[0-9a-f]{40}$`, counterparty)

sanctioned_addresses := {lower(trim_space(a)) | some a in data.sanctions.addresses}

sanctions_loaded if count(sanctioned_addresses) > 0

approval_reasons contains "amount_missing" if {
	freezing
	not amount
}

approval_reasons contains "amount_invalid" if {
	freezing
	amount <= 0
}

approval_reasons contains "threshold_invalid" if {
	freezing
	not threshold
}

approval_reasons contains "amount_over_threshold" if {
	freezing
	amount >= threshold
}

approval_reasons contains "counterparty_missing" if {
	freezing
	not counterparty
}

approval_reasons contains "counterparty_invalid" if {
	freezing
	counterparty
	not valid_counterparty
}

approval_reasons contains "sanctions_list_unavailable" if {
	freezing
	not sanctions_loaded
}

approval_reasons contains "sanctioned_counterparty" if {
	freezing
	counterparty in sanctioned_addresses
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
