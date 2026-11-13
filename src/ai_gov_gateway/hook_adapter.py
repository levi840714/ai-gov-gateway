import logging
import math
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any

from ai_gov_gateway.engine.fingerprint import sha256_hex
from ai_gov_gateway.engine.guardrails.fields import end_user_field
from ai_gov_gateway.engine.guardrails.pii import PseudonymMap, StreamTextRestorer
from ai_gov_gateway.engine.ledger import Settlement
from ai_gov_gateway.engine.llm_gate import LlmRequest
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


# LiteLLM 不論設定一律讀這兩個 header 當 end user id，client 帶了就拒絕
END_USER_HEADERS = ("x-litellm-customer-id", "x-litellm-end-user-id")


def _raw_headers(data: Mapping[str, Any]) -> dict[str, str]:
    """原始 header 只在 secret_fields，proxy_server_request 已遮蔽（M5 spike B1）。"""
    secret = data.get("secret_fields")
    headers = secret.get("raw_headers") if isinstance(secret, Mapping) else None
    if not isinstance(headers, Mapping):
        return {}
    return {str(k).lower(): v for k, v in headers.items() if isinstance(v, str)}


def user_token_from_hook(data: Mapping[str, Any]) -> str | None:
    return _bearer(_raw_headers(data).get("authorization"))


def end_user_header(data: Mapping[str, Any]) -> str | None:
    headers = _raw_headers(data)
    return next((name for name in END_USER_HEADERS if name in headers), None)


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


def owner_of(user_api_key_dict: Any) -> str | None:
    """呼叫者的識別：LiteLLM 的 token 已是 key 的雜湊，再雜湊一次確保記憶體中不留 key 原文。"""
    for name in ("token", "api_key"):
        value = getattr(user_api_key_dict, name, None)
        if isinstance(value, str) and value:
            return sha256_hex(value)
    return None


def llm_request_from_hook(data: Mapping[str, Any], user_api_key_dict: Any) -> LlmRequest:
    """LiteLLM v1.105.1 acompletion 的 pre-call data；檢查與拒絕都交給 LlmGate。"""
    model, call_id = data.get("model"), data.get("litellm_call_id")
    return LlmRequest(
        key_alias=getattr(user_api_key_dict, "key_alias", None),
        owner=owner_of(user_api_key_dict),
        user_token=user_token_from_hook(data),
        end_user_override=end_user_header(data) or end_user_field(client_body(data)),
        model=model if isinstance(model, str) else None,
        call_id=call_id if isinstance(call_id, str) else None,
        messages=data.get("messages") or [],
        max_tokens=data.get("max_tokens"),
        max_completion_tokens=data.get("max_completion_tokens"),
    )


def settlement_from_log(kwargs: Mapping[str, Any], *, at: datetime) -> Settlement | None:
    """success logging 事件的實際用量（M5 spike B3）；欄位缺漏或型別不符時回傳 None。"""
    # standard_logging_object 的 id 不是 call_id，要用 kwargs 的 litellm_call_id
    call_id = kwargs.get("litellm_call_id")
    log = kwargs.get("standard_logging_object")
    if not isinstance(call_id, str) or not call_id or not isinstance(log, Mapping):
        return None
    metadata = log.get("metadata")
    key_hash = metadata.get("user_api_key_hash") if isinstance(metadata, Mapping) else None
    cost = _amount(log.get("response_cost"))
    prompt, completion = _count(log.get("prompt_tokens")), _count(log.get("completion_tokens"))
    if not isinstance(key_hash, str) or not key_hash:
        return None
    if cost is None or prompt is None or completion is None:
        return None
    return Settlement(
        owner=sha256_hex(key_hash),
        call_id=call_id,
        cost_usd=cost,
        prompt_tokens=prompt,
        completion_tokens=completion,
        at=at,
        settled_by="callback",
    )


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _amount(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    # 成本是有二進位誤差的 float；先四捨五入，帳本才會與 SpendLogs.spend 相同
    return Decimal(str(round(value, 12)))


class DuplicateCallId(Exception):
    """同一個呼叫者重複使用 call_id；call_id 可由 client 以 x-litellm-call-id 指定。"""


class PseudonymsEvicted(Exception):
    """對照表在回應前就被回收；照原樣回傳會把代號交給 client，因此改為失敗。"""


PendingKey = tuple[str, str]


class PendingPseudonyms:
    """pre-call 到 post-call 之間的對照表；只在記憶體，數量、原值總長度與存放時間都有上限。

    rate limit 觸發的 fallback 會留下收不到後續 hook 的 call_id，只能靠上限回收。
    """

    def __init__(
        self,
        *,
        max_entries: int = 1000,
        max_value_chars: int = 20_000_000,
        ttl_seconds: float = 600.0,
        max_evicted: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._entries: OrderedDict[PendingKey, tuple[float, PseudonymMap]] = OrderedDict()
        self._evicted: OrderedDict[PendingKey, None] = OrderedDict()
        self._value_chars = 0
        self._max_entries = max_entries
        self._max_value_chars = max_value_chars
        self._ttl = ttl_seconds
        self._max_evicted = max_evicted
        self._clock = clock

    def __len__(self) -> int:
        return len(self._entries)

    def remember(self, owner: str, call_id: str, pseudonyms: PseudonymMap) -> None:
        key = (owner, call_id)
        if key in self._entries or key in self._evicted:
            raise DuplicateCallId()
        # 編譯好的比對樹遠大於原值本身，post-call 只需要代號與原值
        pseudonyms.discard_cache()
        now = self._clock()
        self._entries[key] = (now, pseudonyms)
        self._value_chars += pseudonyms.value_chars
        expired = 0
        while self._entries and self._oldest_time() <= now - self._ttl:
            self._evict()
            expired += 1
        evicted = 0
        while len(self._entries) > self._max_entries or self._value_chars > self._max_value_chars:
            self._evict()
            evicted += 1
        if expired or evicted:
            logger.warning(
                "dropped pending pseudonym maps: expired=%d evicted=%d", expired, evicted
            )

    def pop(self, owner: str | None, call_id: object) -> PseudonymMap | None:
        """沒有對照表時回傳 None；曾經有但已被回收時丟出 PseudonymsEvicted。"""
        key = self._key(owner, call_id)
        if key is None:
            return None
        entry = self._entries.pop(key, None)
        if entry is not None:
            self._value_chars -= entry[1].value_chars
            return entry[1]
        if key in self._evicted:
            del self._evicted[key]
            raise PseudonymsEvicted()
        return None

    def discard(self, owner: str | None, call_id: object) -> None:
        """請求失敗時清掉；不論是否已被回收都不需要再還原。"""
        try:
            self.pop(owner, call_id)
        except PseudonymsEvicted:
            pass

    def _oldest_time(self) -> float:
        return next(iter(self._entries.values()))[0]

    def _evict(self) -> None:
        key, (_, pseudonyms) = self._entries.popitem(last=False)
        self._value_chars -= pseudonyms.value_chars
        self._evicted[key] = None
        while len(self._evicted) > self._max_evicted:
            self._evicted.popitem(last=False)

    @staticmethod
    def _key(owner: str | None, call_id: object) -> PendingKey | None:
        if not owner or not isinstance(call_id, str) or not call_id:
            return None
        return (owner, call_id)


class AdmittedCalls:
    """這個 process 放行過、還沒結束的請求；failure hook 只釋放其中的請求。

    client 可用 x-litellm-call-id 沿用進行中的 call_id；被拒絕的那個請求也會觸發 failure hook，
    所以記下拒絕的次數，先抵銷這些 failure，才不會釋放原本那筆。
    """

    def __init__(
        self,
        *,
        max_entries: int = 10_000,
        ttl_seconds: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        # (owner, call_id) → (放行時間, 尚未抵銷的拒絕次數)
        self._entries: OrderedDict[PendingKey, tuple[float, int]] = OrderedDict()
        self._max_entries = max_entries
        self._ttl = ttl_seconds
        self._clock = clock

    def __len__(self) -> int:
        return len(self._entries)

    def add(self, owner: str | None, call_id: object) -> None:
        key = PendingPseudonyms._key(owner, call_id)
        if key is None:
            return
        self._expire()
        self._entries[key] = (self._clock(), 0)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def rejected(self, owner: str | None, call_id: object) -> None:
        """pre-call 拒絕了沿用這個 call_id 的另一個請求。"""
        key = PendingPseudonyms._key(owner, call_id)
        entry = self._entries.get(key) if key else None
        if key is not None and entry is not None:
            self._entries[key] = (entry[0], entry[1] + 1)

    def pop_for_release(self, owner: str | None, call_id: object) -> bool:
        """failure hook 用：是這個 process 放行的請求才回傳 True，且只回傳一次。"""
        self._expire()
        key = PendingPseudonyms._key(owner, call_id)
        entry = self._entries.get(key) if key else None
        if key is None or entry is None:
            return False
        if entry[1]:
            self._entries[key] = (entry[0], entry[1] - 1)
            return False
        del self._entries[key]
        return True

    def forget(self, owner: str | None, call_id: object) -> None:
        """請求已成功結束；之後同一個 call_id 的 failure 都不是這個請求的。"""
        key = PendingPseudonyms._key(owner, call_id)
        if key is not None:
            self._entries.pop(key, None)

    def _expire(self) -> None:
        cutoff = self._clock() - self._ttl
        while self._entries and next(iter(self._entries.values()))[0] <= cutoff:
            self._entries.popitem(last=False)


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
