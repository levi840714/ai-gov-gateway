"""chat 請求的欄位白名單：guardrail 只遮蔽訊息內容與 tool 參數，其他欄位會原樣送給 provider。

tools、response_format 的 schema 與 stop 由開發者定義，允許但不遮蔽；圖片 URL 也不遮蔽。
這些都是已知限制。
"""

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
