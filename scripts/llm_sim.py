"""LLM guardrail 的 demo 與整合測試共用：呼叫 chat completions、讀取 probe_llm 的紀錄。"""

import json
from typing import Any

import httpx

from scripts.env import KEYS_FILE, LITELLM_URL

PROBE_URL = "http://127.0.0.1:8300"


def chat(
    key_alias: str,
    messages: list[dict[str, Any]],
    *,
    model: str = "probe-primary",
    stream: bool = False,
) -> httpx.Response:
    key = json.loads(KEYS_FILE.read_text())[key_alias]
    return httpx.post(
        f"{LITELLM_URL}/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": model, "messages": messages, "stream": stream},
        timeout=60,
    )


def stream_text(response: httpx.Response) -> tuple[str, str]:
    """回傳（串接後的文字, 串接後的 tool call 參數）。"""
    text, arguments = "", ""
    for line in response.text.splitlines():
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        for choice in json.loads(line[6:]).get("choices", []):
            delta = choice.get("delta") or {}
            text += delta.get("content") or ""
            for call in delta.get("tool_calls") or []:
                arguments += (call.get("function") or {}).get("arguments") or ""
    return text, arguments


def probe_requests() -> list[dict[str, Any]]:
    return list(httpx.get(f"{PROBE_URL}/requests", timeout=10).json())


def clear_probe() -> None:
    httpx.delete(f"{PROBE_URL}/requests", timeout=10).raise_for_status()


def tool_turn(
    content: str, *, system: str = "probe:reply=ok", name: str = "get_counterparties"
) -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "調查 0x1111111111111111111111111111111111111111"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": name, "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": content},
    ]
