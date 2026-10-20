from typing import Any

from litellm.integrations.custom_guardrail import CustomGuardrail


class GovernanceHook(CustomGuardrail):
    """V7 驗證用：echo 的 text 含 freeze 時阻擋，並記錄 hook 能取得的身份資訊。"""

    async def async_pre_call_hook(
        self, user_api_key_dict: Any, cache: Any, data: dict, call_type: str
    ) -> dict:
        tool = data.get("mcp_tool_name")
        arguments = data.get("mcp_arguments") or {}
        print(
            "governance_hook",
            call_type,
            f"key_alias={getattr(user_api_key_dict, 'key_alias', None)}",
            f"tool={tool}",
            f"args={arguments}",
            f"has_user_token={bool(data.get('incoming_bearer_token'))}",
            flush=True,
        )
        if call_type == "call_mcp_tool" and tool == "echo" and "freeze" in str(arguments.get("text")):
            raise ValueError("pending_approval: approval_id=demo-123")
        return data
