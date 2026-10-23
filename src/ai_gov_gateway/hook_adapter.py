import logging
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from typing import Any

from ai_gov_gateway.engine.guardrails.pii import PseudonymMap, StreamTextRestorer
from ai_gov_gateway.engine.models import ToolCall

logger = logging.getLogger(__name__)


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


def client_body(data: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """LiteLLM v1.105.1 把 client 的 body（加上它自己的 metadata）放在 proxy_server_request。"""
    request = data.get("proxy_server_request")
    body = request.get("body") if isinstance(request, Mapping) else None
    return body if isinstance(body, Mapping) else None


class PendingPseudonyms:
    """pre-call 到 post-call 之間的對照表；只在記憶體，數量與存放時間都有上限。

    rate limit 觸發的 fallback 會留下收不到後續 hook 的 call_id，只能靠上限回收。
    """

    def __init__(
        self,
        *,
        max_entries: int = 1000,
        ttl_seconds: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._entries: OrderedDict[str, tuple[float, PseudonymMap]] = OrderedDict()
        self._max_entries = max_entries
        self._ttl = ttl_seconds
        self._clock = clock

    def __len__(self) -> int:
        return len(self._entries)

    def remember(self, call_id: str, pseudonyms: PseudonymMap) -> None:
        now = self._clock()
        expired = 0
        while self._entries and next(iter(self._entries.values()))[0] <= now - self._ttl:
            self._entries.popitem(last=False)
            expired += 1
        self._entries.pop(call_id, None)
        self._entries[call_id] = (now, pseudonyms)
        evicted = 0
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)
            evicted += 1
        if expired or evicted:
            logger.warning(
                "dropped pending pseudonym maps: expired=%d evicted=%d", expired, evicted
            )

    def pop(self, call_id: object) -> PseudonymMap | None:
        if not isinstance(call_id, str):
            return None
        entry = self._entries.pop(call_id, None)
        return entry[1] if entry else None


class RestoreError(Exception):
    """還原代號失敗；訊息只有例外類別名稱，hook 轉成 502。"""


def restore_response(response: Any, pseudonyms: PseudonymMap) -> None:
    """把 LiteLLM 回應中的代號換回原值；直接修改物件。"""
    for choice in getattr(response, "choices", None) or []:
        message = getattr(choice, "message", None)
        if message is None:
            continue
        if isinstance(getattr(message, "content", None), str):
            message.content = pseudonyms.restore(message.content)
        functions = [getattr(call, "function", None) for call in _tool_calls(message)]
        functions.append(getattr(message, "function_call", None))
        for function in functions:
            if function is not None and isinstance(getattr(function, "arguments", None), str):
                function.arguments = pseudonyms.restore_json(function.arguments)


async def restore_stream(
    chunks: AsyncIterator[Any], pseudonyms: PseudonymMap
) -> AsyncIterator[Any]:
    """逐段還原 streaming 回應。

    多留一個 chunk 再送出：串流沒有 finish_reason 就結束時，暫存的文字才有地方放。
    """
    restorer = _StreamRestorer(pseudonyms)
    last: Any = None
    async for chunk in chunks:
        for ready in _safely(restorer.push, chunk):
            if last is not None:
                yield last
            last = ready
    for ready in _safely(restorer.release):
        if last is not None:
            yield last
        last = ready
    if last is not None:
        _safely(restorer.flush_text, last)
        yield last


def _safely[T](work: Callable[..., T], *args: Any) -> T:
    try:
        return work(*args)
    except Exception as error:
        raise RestoreError(type(error).__name__) from error


def _choices(chunk: Any) -> list[tuple[int, Any, Any]]:
    return [
        (getattr(choice, "index", 0), choice, getattr(choice, "delta", None))
        for choice in getattr(chunk, "choices", None) or []
    ]


def _tool_calls(holder: Any) -> list[Any]:
    calls = getattr(holder, "tool_calls", None) if holder is not None else None
    return list(calls) if calls else []


class _StreamRestorer:
    """文字逐段還原；tool call 的 chunk 暫存到每個有 tool call 的 choice 都結束才還原參數。"""

    def __init__(self, pseudonyms: PseudonymMap) -> None:
        self._pseudonyms = pseudonyms
        self._texts: dict[int, StreamTextRestorer] = {}
        self._held: list[Any] = []
        self._calls: dict[tuple[int, int], list[Any]] = {}
        self._open: set[int] = set()

    def push(self, chunk: Any) -> list[Any]:
        for index, choice, delta in _choices(chunk):
            restorer = self._texts.setdefault(index, StreamTextRestorer(self._pseudonyms))
            if isinstance(getattr(delta, "content", None), str):
                delta.content = restorer.feed(delta.content)
            for call in _tool_calls(delta):
                if getattr(call, "function", None) is not None:
                    key = (index, getattr(call, "index", 0))
                    self._calls.setdefault(key, []).append(call.function)
                    self._open.add(index)
            if getattr(choice, "finish_reason", None) is not None:
                self._open.discard(index)
                if delta is not None and (rest := restorer.flush()):
                    delta.content = (delta.content or "") + rest
        if not self._calls and not self._held:
            return [chunk]
        self._held.append(chunk)
        # 其他 choice 的參數可能還沒收完，提早還原會把半個代號當成無效 JSON 原樣送出
        return [] if self._open else self.release()

    def release(self) -> list[Any]:
        for functions in self._calls.values():
            # client 依 index 串接參數片段：完整還原後的參數放在第一段，其餘清空
            full = "".join(f.arguments for f in functions if isinstance(f.arguments, str))
            for position, function in enumerate(functions):
                function.arguments = self._pseudonyms.restore_json(full) if position == 0 else ""
        held, self._held, self._calls, self._open = self._held, [], {}, set()
        return held

    def flush_text(self, chunk: Any) -> None:
        # 最後一個 chunk 沒有該 choice 時（例如只帶 usage）無處可放，暫存的文字會被丟棄
        for index, _, delta in _choices(chunk):
            restorer = self._texts.pop(index, None)
            if restorer is not None and delta is not None and (rest := restorer.flush()):
                delta.content = (delta.content or "") + rest
