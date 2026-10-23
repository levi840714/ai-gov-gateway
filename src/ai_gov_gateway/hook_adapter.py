from collections.abc import AsyncIterator, Iterable, Mapping
from typing import Any

from ai_gov_gateway.engine.guardrails.pii import PseudonymMap, StreamTextRestorer
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


def restore_response(response: Any, pseudonyms: PseudonymMap) -> None:
    """把 LiteLLM 回應中的代號換回原值；直接修改物件。"""
    for choice in getattr(response, "choices", None) or []:
        message = getattr(choice, "message", None)
        if message is None:
            continue
        if isinstance(getattr(message, "content", None), str):
            message.content = pseudonyms.restore(message.content)
        for call in getattr(message, "tool_calls", None) or []:
            function = getattr(call, "function", None)
            if function is not None and isinstance(function.arguments, str):
                function.arguments = pseudonyms.restore_json(function.arguments)


async def restore_stream(
    chunks: AsyncIterator[Any], pseudonyms: PseudonymMap
) -> AsyncIterator[Any]:
    """文字以 StreamTextRestorer 逐段還原；出現 tool call 後暫存 chunk，到結束時一次還原參數。"""
    texts: dict[int, StreamTextRestorer] = {}
    held: list[Any] = []
    calls: dict[tuple[int, int], list[Any]] = {}
    async for chunk in chunks:
        finished = False
        for choice in getattr(chunk, "choices", None) or []:
            index = getattr(choice, "index", 0)
            delta = getattr(choice, "delta", None)
            restorer = texts.setdefault(index, StreamTextRestorer(pseudonyms))
            if delta is not None and isinstance(getattr(delta, "content", None), str):
                delta.content = restorer.feed(delta.content)
            for call in (getattr(delta, "tool_calls", None) or []) if delta is not None else []:
                if getattr(call, "function", None) is not None:
                    key = (index, getattr(call, "index", 0))
                    calls.setdefault(key, []).append(call.function)
            if getattr(choice, "finish_reason", None) is not None:
                finished = True
                if delta is not None and (rest := restorer.flush()):
                    delta.content = (delta.content or "") + rest
        if not calls and not held:
            yield chunk
            continue
        held.append(chunk)
        if finished:
            _restore_arguments(calls, pseudonyms)
            for item in held:
                yield item
            held, calls = [], {}
    _restore_arguments(calls, pseudonyms)
    for item in held:
        yield item


def _restore_arguments(calls: dict[tuple[int, int], list[Any]], pseudonyms: PseudonymMap) -> None:
    # client 依 index 串接參數片段：完整還原後的參數放在第一段，其餘清空
    for functions in calls.values():
        full = "".join(f.arguments for f in functions if isinstance(f.arguments, str))
        for position, function in enumerate(functions):
            function.arguments = pseudonyms.restore_json(full) if position == 0 else ""
