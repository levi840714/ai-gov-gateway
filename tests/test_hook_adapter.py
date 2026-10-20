from ai_gov_gateway.hook_adapter import tool_call_from_hook

# LiteLLM v1.105.1 call_mcp_tool 的 data 欄位（M1 V7 實際觀察）
M1_DATA = {
    "messages": [],
    "model": "x",
    "mcp_tool_name": "freeze_account",
    "mcp_arguments": {"account_id": "acct-001", "amount_usd": 25000},
    "mcp_server_name": "account_ops",
    "incoming_bearer_token": "eyJ.token.sig",
    "metadata": {},
}


def test_maps_litellm_fields():
    call = tool_call_from_hook(M1_DATA, "kyt-agent@1.4")
    assert call.key_alias == "kyt-agent@1.4"
    assert (call.server, call.tool) == ("account_ops", "freeze_account")
    assert call.arguments == {"account_id": "acct-001", "amount_usd": 25000}
    assert call.user_token == "eyJ.token.sig"


def test_strips_bearer_prefix():
    data = M1_DATA | {"incoming_bearer_token": "Bearer eyJ.token.sig"}
    assert tool_call_from_hook(data, None).user_token == "eyJ.token.sig"


def test_missing_fields_become_empty_and_fail_closed_downstream():
    call = tool_call_from_hook({}, None)
    assert (call.server, call.tool, call.arguments, call.user_token) == ("", "", {}, None)


def test_non_string_token_becomes_none():
    assert tool_call_from_hook(M1_DATA | {"incoming_bearer_token": 123}, None).user_token is None


def test_non_dict_arguments_become_empty():
    assert tool_call_from_hook(M1_DATA | {"mcp_arguments": ["a"]}, None).arguments == {}
