"""M5 驗證：記錄依使用者記帳需要的 LiteLLM v1.105.1 hook 行為（B1–B6、B10）。

只記錄鍵名、布林與數字，不記 token、key 或訊息內容。pre-call 把 max_tokens 改成 17（B10）。
"""

import hashlib
import itertools
import json
import logging
import re
from collections.abc import AsyncIterator, Mapping
from typing import Any

from litellm.integrations.custom_guardrail import CustomGuardrail

logger = logging.getLogger("m5_spike")
_seq = itertools.count()
_JWT = re.compile(r"^(Bearer\s+)?eyJ[\w-]+\.[\w-]+\.[\w-]+$")
SPIKE_MAX_TOKENS = 17


def _log(tag: str, payload: dict[str, Any]) -> None:
    payload = {"seq": next(_seq), **payload}
    logger.warning("M5SPIKE %s %s", tag, json.dumps(payload, ensure_ascii=False, default=str))


def _short_hash(value: object) -> str | None:
    # 只留雜湊的前 8 碼，用來比對各處的 key 雜湊是否同源
    if not isinstance(value, str) or not value:
        return None
    return hashlib.sha256(value.encode()).hexdigest()[:8]


def _raw_headers(data: Mapping[str, Any]) -> Mapping[str, Any] | None:
    secret = data.get("secret_fields")
    if isinstance(secret, Mapping):
        headers = secret.get("raw_headers")
    else:
        headers = getattr(secret, "raw_headers", None)
    return headers if isinstance(headers, Mapping) else None


class M5SpikeGuardrail(CustomGuardrail):
    async def async_pre_call_hook(
        self, user_api_key_dict: Any, cache: Any, data: dict[str, Any], call_type: str
    ) -> dict[str, Any]:
        headers = _raw_headers(data)
        lowered = {str(k).lower(): v for k, v in (headers or {}).items()}
        authorization = lowered.get("authorization")
        _log(
            "pre_call",
            {
                "call_type": call_type,
                "call_id": data.get("litellm_call_id"),
                "model": data.get("model"),
                "stream": data.get("stream"),
                "secret_fields_type": type(data.get("secret_fields")).__name__,
                "raw_header_names": sorted(lowered),
                "authorization_is_jwt": isinstance(authorization, str)
                and bool(_JWT.match(authorization.strip())),
                "max_tokens_in": data.get("max_tokens"),
                "key_token_hash": _short_hash(getattr(user_api_key_dict, "token", None)),
            },
        )
        if call_type == "acompletion":
            data["max_tokens"] = SPIKE_MAX_TOKENS
        return data

    async def async_log_success_event(
        self, kwargs: dict[str, Any], response_obj: Any, start_time: Any, end_time: Any
    ) -> None:
        slo = kwargs.get("standard_logging_object") or {}
        metadata = slo.get("metadata") or {}
        _log(
            "log_success",
            {
                "call_id": kwargs.get("litellm_call_id"),
                "slo_id": slo.get("id"),
                "response_cost": kwargs.get("response_cost"),
                "slo_response_cost": slo.get("response_cost"),
                "prompt_tokens": slo.get("prompt_tokens"),
                "completion_tokens": slo.get("completion_tokens"),
                "total_tokens": slo.get("total_tokens"),
                "model": slo.get("model"),
                "model_group": slo.get("model_group"),
                "stream": slo.get("stream"),
                "key_hash_hash": _short_hash(metadata.get("user_api_key_hash")),
                "slo_keys": sorted(slo),
            },
        )

    async def async_log_failure_event(
        self, kwargs: dict[str, Any], response_obj: Any, start_time: Any, end_time: Any
    ) -> None:
        exception = kwargs.get("exception")
        _log(
            "log_failure",
            {
                "call_id": kwargs.get("litellm_call_id"),
                "model": kwargs.get("model"),
                "error": type(exception).__name__ if exception else None,
            },
        )

    async def async_post_call_failure_hook(
        self,
        request_data: dict[str, Any],
        original_exception: Exception,
        user_api_key_dict: Any,
        traceback_str: str | None = None,
    ) -> None:
        _log(
            "failure_hook",
            {
                "call_id": request_data.get("litellm_call_id"),
                "error": type(original_exception).__name__,
                "status": getattr(original_exception, "status_code", None),
            },
        )

    async def async_post_call_success_hook(
        self, data: dict[str, Any], user_api_key_dict: Any, response: Any
    ) -> Any:
        _log("success_hook", {"call_id": data.get("litellm_call_id")})
        return response

    async def async_post_call_streaming_iterator_hook(
        self, user_api_key_dict: Any, response: Any, request_data: dict[str, Any]
    ) -> AsyncIterator[Any]:
        call_id = request_data.get("litellm_call_id")
        _log("stream", {"call_id": call_id})
        count = 0
        try:
            async for chunk in response:
                count += 1
                yield chunk
        finally:
            _log("stream_end", {"call_id": call_id, "chunks": count})
