"""M4 驗證：記錄 LiteLLM v1.105.1 的 LLM hook 行為，以 SPIKE_SECRET 模擬假名化與還原。"""

import itertools
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import HTTPException
from litellm.integrations.custom_guardrail import CustomGuardrail

logger = logging.getLogger("m4_spike")
# 跨 hook 的遞增序號：判斷 streaming 時 post_call 與 stream 的先後
_seq = itertools.count()


def _log(tag: str, payload: dict[str, Any]) -> None:
    payload = {"seq": next(_seq), **payload}
    logger.warning("M4SPIKE %s %s", tag, json.dumps(payload, ensure_ascii=False, default=str))


class SpikeGuardrail(CustomGuardrail):
    async def async_pre_call_hook(
        self, user_api_key_dict: Any, cache: Any, data: dict[str, Any], call_type: str
    ) -> dict[str, Any]:
        messages = data.get("messages") or []
        _log(
            "pre_call",
            {
                "call_type": call_type,
                "call_id": data.get("litellm_call_id"),
                "key_alias": getattr(user_api_key_dict, "key_alias", None),
                "model": data.get("model"),
                "stream": data.get("stream"),
                # 只含驗證用的測試字串：觀察 fallback 重跑 pre_call 時看到的是原文還是改寫後的內容
                "last_content": str(messages[-1].get("content"))[:60] if messages else None,
                "keys": sorted(data),
                "metadata_keys": sorted(data.get("metadata") or {}),
            },
        )
        if any("SPIKE_DENY" in str(m.get("content")) for m in messages):
            raise HTTPException(
                status_code=403, detail={"governance": "denied", "reasons": ["spike_denied"]}
            )
        for message in messages:
            if isinstance(message.get("content"), str):
                message["content"] = message["content"].replace("SPIKE_SECRET", "<SPIKE_1>")
        return data

    async def async_post_call_success_hook(
        self, data: dict[str, Any], user_api_key_dict: Any, response: Any
    ) -> Any:
        _log(
            "post_call",
            {
                "call_id": data.get("litellm_call_id"),
                "type": type(response).__name__,
                "stream": data.get("stream"),
            },
        )
        for choice in getattr(response, "choices", []):
            message = getattr(choice, "message", None)
            if message is None:
                continue
            if isinstance(message.content, str):
                message.content = message.content.replace("<SPIKE_1>", "SPIKE_SECRET")
            for call in message.tool_calls or []:
                call.function.arguments = call.function.arguments.replace(
                    "<SPIKE_1>", "SPIKE_SECRET"
                )
        return response

    async def async_post_call_failure_hook(
        self,
        request_data: dict[str, Any],
        original_exception: Exception,
        user_api_key_dict: Any,
        traceback_str: str | None = None,
    ) -> None:
        _log(
            "failure",
            {
                "call_id": request_data.get("litellm_call_id"),
                "error": type(original_exception).__name__,
                "status": getattr(original_exception, "status_code", None),
            },
        )

    async def async_post_call_streaming_iterator_hook(
        self, user_api_key_dict: Any, response: Any, request_data: dict[str, Any]
    ) -> AsyncIterator[Any]:
        _log("stream", {"call_id": request_data.get("litellm_call_id")})
        count = 0
        async for chunk in response:
            count += 1
            for choice in getattr(chunk, "choices", []):
                delta = choice.delta
                if isinstance(getattr(delta, "content", None), str):
                    delta.content = delta.content.replace("<SPIKE_1>", "SPIKE_SECRET")
            yield chunk
        _log("stream_end", {"call_id": request_data.get("litellm_call_id"), "chunks": count})
