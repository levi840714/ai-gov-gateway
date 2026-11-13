"""chat 請求的欄位白名單：guardrail 只遮蔽訊息內容與 tool 參數，其他欄位會原樣送給 provider。

tools、response_format 的 schema 與 stop 由開發者定義，允許但不遮蔽；圖片 URL 也不遮蔽。
這些都是已知限制。
"""

import json
import re
from collections.abc import Mapping
from typing import Any

from ai_gov_gateway.engine.guardrails.models import GuardrailError

REQUEST_FIELDS = frozenset(
    {
        "model",
        "messages",
        "stream",
        "stream_options",
        "temperature",
        "top_p",
        "max_tokens",
        "max_completion_tokens",
        "n",
        "stop",
        "seed",
        # 由 LlmGate 以 end_user_override 拒絕並寫稽核；在這裡拒絕就不會留下稽核
        "user",
        "tools",
        "tool_choice",
        "parallel_tool_calls",
        "response_format",
        "metadata",
    }
)
# 取不到 client 原始 body 時只能檢查 data；data 含 LiteLLM 自己的欄位，所以改列已知會被轉送的欄位
FORWARDED_FIELDS = frozenset(
    {
        "prompt",
        "system",
        "input",
        "instructions",
        "suffix",
        "extra_body",
        "prediction",
        "functions",
        "function_call",
        "audio",
        "documents",
        "web_search_options",
    }
)
MESSAGE_FIELDS = frozenset({"role", "content", "name", "tool_calls", "tool_call_id"})
TOOL_CALL_FIELDS = frozenset({"id", "type", "function"})
FUNCTION_FIELDS = frozenset({"name", "arguments"})
PART_FIELDS = {"text": frozenset({"type", "text"}), "image_url": frozenset({"type", "image_url"})}
IMAGE_FIELDS = frozenset({"url", "detail"})
IMAGE_DETAILS = frozenset({"low", "high", "auto"})
# 錯誤只回欄位名稱；名稱也來自 client，不像欄位名稱時不回傳，以免把內容帶進回應與日誌
_FIELD_NAME = re.compile(r"[a-z_]{1,40}")


def check_request(body: Mapping[str, Any] | None, data: Mapping[str, Any]) -> None:
    """body 是 client 送來的原始內容（可能為 None），data 是 LiteLLM 要轉送的請求。"""
    if body is not None:
        _only(body, REQUEST_FIELDS, "")
    else:
        for name in FORWARDED_FIELDS:
            if not _empty(data.get(name)):
                raise _unsupported("", name)
    messages = data.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if isinstance(message, Mapping):
                _check_message(message)


def end_user_field(body: Mapping[str, Any] | None) -> str | None:
    """client 自帶的 end user 身份欄位名稱；LiteLLM 會拿這些欄位當 end user id。"""
    if body is None:
        return None
    if "user" in body:
        return "user"
    metadata = body.get("metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except ValueError:
            return None
    if isinstance(metadata, Mapping):
        return next((f"metadata.{k}" for k in ("user_id", "user") if k in metadata), None)
    return None


def text_length(messages: Any) -> int:
    """訊息文字（含多段內容的文字段）與 tool call 參數的字元數；不含 id 與名稱。"""
    if not isinstance(messages, list):
        return 0
    total = 0
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        content = message.get("content")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            total += sum(
                len(part["text"])
                for part in content
                if isinstance(part, Mapping) and isinstance(part.get("text"), str)
            )
        calls = message.get("tool_calls")
        for call in calls if isinstance(calls, list) else []:
            function = call.get("function") if isinstance(call, Mapping) else None
            arguments = function.get("arguments") if isinstance(function, Mapping) else None
            total += len(arguments) if isinstance(arguments, str) else 0
    return total


def _check_message(message: Mapping[str, Any]) -> None:
    _only(message, MESSAGE_FIELDS, "messages.")
    content = message.get("content")
    if isinstance(content, list):
        for part in content:
            kind = part.get("type") if isinstance(part, Mapping) else None
            if not isinstance(part, Mapping) or kind not in PART_FIELDS:
                raise _unsupported("messages.content.", kind)
            _only(part, PART_FIELDS[kind], f"messages.content.{kind}.")
            if kind == "image_url":
                _check_image(part.get("image_url"))
    elif content is not None and not isinstance(content, str):
        raise _unsupported("messages.", "content")
    calls = message.get("tool_calls")
    for call in calls if isinstance(calls, list) else []:
        if isinstance(call, Mapping):
            _only(call, TOOL_CALL_FIELDS, "messages.tool_calls.")
            function = call.get("function")
            if isinstance(function, Mapping):
                _only(function, FUNCTION_FIELDS, "messages.tool_calls.function.")


def _check_image(image: Any) -> None:
    prefix = "messages.content.image_url."
    if not isinstance(image, Mapping) or not isinstance(image.get("url"), str):
        raise _unsupported(prefix, "image_url")
    _only(image, IMAGE_FIELDS, f"{prefix}image_url.")
    if image.get("detail") is not None and image.get("detail") not in IMAGE_DETAILS:
        raise _unsupported(f"{prefix}image_url.", "detail")


def _only(fields: Mapping[str, Any], allowed: frozenset[str], prefix: str) -> None:
    for name, value in fields.items():
        # 值為空的欄位不帶任何內容，例如 SDK 序列化回應時留下的 "refusal": null
        if name not in allowed and not _empty(value):
            raise _unsupported(prefix, name)


def _empty(value: Any) -> bool:
    return value is None or (isinstance(value, str | list | dict) and not value)


def _unsupported(prefix: str, name: object) -> GuardrailError:
    shown = name if isinstance(name, str) and _FIELD_NAME.fullmatch(name) else "?"
    return GuardrailError(400, "unsupported_field", field=prefix + shown)
