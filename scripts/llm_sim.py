"""LLM guardrail 的 demo 與整合測試共用：呼叫 chat completions、讀取 probe_llm 的紀錄。"""

import json
import time
from typing import Any

import httpx

from scripts.env import KEYS_FILE, LITELLM_URL
from scripts.make_keys import KEYS

PROBE_URL = "http://127.0.0.1:8300"
# demo 與整合測試使用正式版本的 key，搭配 probe 模型
AGENT_KEY = "kyt-agent@1.5"


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


def wait_until_allowed(key_alias: str, timeout: float = 90) -> None:
    """LiteLLM 的 rpm、tpm 以 60 秒的固定視窗計算；被限流時等到視窗過去、再次放行。"""
    ping = [{"role": "system", "content": "probe:reply=ok"}, {"role": "user", "content": "ping"}]
    deadline = time.monotonic() + timeout
    while chat(key_alias, ping).status_code == 429:
        if time.monotonic() > deadline:
            raise RuntimeError(f"{key_alias} is still rate limited after {timeout}s")
        time.sleep(5)


def exceed_tpm(key_alias: str = AGENT_KEY, attempts: int = 3) -> list[int]:
    """每次回報的用量都超過 key 的 tpm；前後都等限流解除，不影響之後的請求。"""
    usage = KEYS[key_alias]["tpm_limit"] + 1
    messages = [
        {"role": "system", "content": f"probe:usage={usage}"},
        {"role": "user", "content": "x"},
    ]
    wait_until_allowed(key_alias)
    try:
        return [chat(key_alias, messages).status_code for _ in range(attempts)]
    finally:
        wait_until_allowed(key_alias)


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
