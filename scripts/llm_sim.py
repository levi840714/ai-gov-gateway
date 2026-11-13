"""LLM guardrail 的 demo 與整合測試共用：呼叫 chat completions、讀取 probe_llm 的紀錄。"""

import json
import time
from typing import Any

import httpx

from scripts.env import KEYS_FILE, LITELLM_URL
from scripts.kc import user_token
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
    user: str | None = "alice",
    token: str | None = None,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """以 x-litellm-api-key 帶 agent 的 key、Authorization 帶委託使用者的 token（M5）。

    token 未指定時每次重取：access token 只有 300 秒。user 為 None 時不帶 Authorization。
    """
    key = json.loads(KEYS_FILE.read_text())[key_alias]
    sent = {"x-litellm-api-key": f"Bearer {key}", **(headers or {})}
    if token is None and user is not None:
        token = user_token(user)
    if token is not None:
        sent["Authorization"] = f"Bearer {token}"
    return httpx.post(
        f"{LITELLM_URL}/v1/chat/completions",
        headers=sent,
        json={"model": model, "messages": messages, "stream": stream, **(body or {})},
        timeout=60,
    )


# 等待與超過 key 限流用的請求：模型沒有 fallback，LiteLLM 限流時只會跑一次 pre-call 並釋放。
# probe-primary 會觸發限流 fallback，第二個 call_id 收不到事件（M5 spike B5），留在帳本佔同時進行數
LIMIT_MODEL = "probe-fallback"
# 等待用 bob：不消耗 alice 的個人 rpm。超過 tpm 的每一筆換一位使用者：放行的那筆也會超過
# 該使用者的個人 tpm（M5），下一筆若同一人會被 gateway 擋下，就看不到 key 的限流
WAIT_USER = "bob"
TPM_USERS = ("carol", "bob", "alice")


def key_limited(response: httpx.Response) -> bool:
    """LiteLLM 的 key 限流；gateway 的個人限流（user_rate_limited）不算。"""
    return response.status_code == 429 and "user_rate_limited" not in response.text


def wait_until_allowed(key_alias: str, timeout: float = 90, user: str = WAIT_USER) -> None:
    """LiteLLM 的 rpm、tpm 以 60 秒的固定視窗計算；被限流時等到視窗過去、再次放行。

    每次探測都會在帳本留下一筆並計入該使用者的 rpm，所以間隔 10 秒。
    """
    ping = [{"role": "system", "content": "probe:reply=ok"}, {"role": "user", "content": "ping"}]
    deadline = time.monotonic() + timeout
    while chat(key_alias, ping, model=LIMIT_MODEL, user=user).status_code == 429:
        if time.monotonic() > deadline:
            raise RuntimeError(f"{key_alias} is still rate limited after {timeout}s")
        time.sleep(10)


def exceed_tpm(key_alias: str = AGENT_KEY) -> list[httpx.Response]:
    """每次回報的用量都超過 key 的 tpm；前後都等限流解除，不影響之後的請求。"""
    usage = KEYS[key_alias]["tpm_limit"] + 1
    messages = [
        {"role": "system", "content": f"probe:usage={usage}"},
        {"role": "user", "content": "x"},
    ]
    # 連續執行時，上一輪使用者的個人 tpm（滑動視窗）可能比 key 的固定視窗晚解除
    for user in TPM_USERS:
        wait_until_allowed(key_alias, user=user)
    try:
        return [chat(key_alias, messages, model=LIMIT_MODEL, user=user) for user in TPM_USERS]
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
