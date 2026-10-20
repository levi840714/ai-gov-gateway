from collections.abc import Iterable, Mapping
from typing import Any

from ai_gov_gateway.engine.models import ToolCall


def has_ambiguous_authorization(header_names: Iterable[str]) -> bool:
    """LiteLLM 以名稱開頭為 authorization 的任一 header 做 OBO，hook 只看 Authorization。

    多於一個時稽核的使用者與上游收到的使用者可能不同，因此只接受單一 Authorization。
    """
    names = [name.lower() for name in header_names if name.lower().startswith("authorization")]
    return names not in ([], ["authorization"])


def _bearer(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    token = value.strip()
    return token[7:].strip() if token.lower().startswith("bearer ") else token


def tool_call_from_hook(data: Mapping[str, Any], key_alias: str | None) -> ToolCall:
    """LiteLLM v1.105.1 pre_mcp_call 的欄位；缺漏時留空，由 policy 判為 unknown_tool。"""
    arguments = data.get("mcp_arguments")
    return ToolCall(
        key_alias=key_alias,
        user_token=_bearer(data.get("incoming_bearer_token")),
        server=str(data.get("mcp_server_name") or ""),
        tool=str(data.get("mcp_tool_name") or ""),
        arguments=dict(arguments) if isinstance(arguments, Mapping) else {},
    )
