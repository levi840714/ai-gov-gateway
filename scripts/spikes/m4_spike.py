"""依序觀察 S2–S5 與 A1–A8，印出 PASS / FAIL / INFO 與細節；結論寫在 docs/spikes/m4-findings.md。

前提：gateway 以 docker-compose.spike.yml 疊加啟動。印出的內容只含測試字串，不含 key。
"""

import json
import re
import time
from typing import Any

import httpx

from scripts.env import KEYS_FILE, LITELLM_URL, env

PROBE = "http://127.0.0.1:8300"
KEYS: dict[str, str] = json.loads(KEYS_FILE.read_text())


def chat(
    key: str, *system: str, user: str, model: str = "probe-primary", stream: bool = False
) -> httpx.Response:
    messages = [{"role": "system", "content": line} for line in system]
    messages.append({"role": "user", "content": user})
    return httpx.post(
        f"{LITELLM_URL}/v1/chat/completions",
        headers={"Authorization": f"Bearer {KEYS[key]}"},
        json={"model": model, "messages": messages, "stream": stream},
        timeout=60,
    )


def recorded() -> list[dict[str, Any]]:
    return httpx.get(f"{PROBE}/requests").json()


def clear() -> None:
    httpx.delete(f"{PROBE}/requests")


def redact(text: str) -> str:
    # 429 的錯誤訊息帶 key 的 hash，不寫進輸出
    return re.sub(r"[0-9a-f]{64}", "<hash>", text)


def report(name: str, ok: bool | None, detail: object) -> None:
    label = "INFO" if ok is None else "PASS" if ok else "FAIL"
    print(f"{label}  {name}  {detail}")


def stream_text(body: str) -> str:
    parts = []
    for line in body.splitlines():
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        for choice in json.loads(line.removeprefix("data: ")).get("choices", []):
            parts.append((choice.get("delta") or {}).get("content") or "")
    return "".join(parts)


def check_rewrite_and_restore(key: str) -> None:
    clear()
    response = chat(key, "probe:reply=echo", user="hello SPIKE_SECRET")
    sent = recorded()[-1]["messages"][-1]["content"]
    content = response.json()["choices"][0]["message"]["content"]
    report("A2 provider 收到改寫後的內容", sent == "hello <SPIKE_1>", sent)
    report("A3 回應內容可還原", content == "hello SPIKE_SECRET", content)


def check_fallback(key: str) -> None:
    clear()
    response = chat(key, "probe:reply=echo", "probe:fail=probe-primary", user="SPIKE_SECRET")
    models = [r["model"] for r in recorded()]
    sent = [r["messages"][-1]["content"] for r in recorded()]
    ok = (
        response.status_code == 200
        and "probe-fallback" in models
        and all("SPIKE_SECRET" not in s for s in sent)
    )
    body = response.json() if response.status_code == 200 else response.text[:200]
    content = body["choices"][0]["message"]["content"] if isinstance(body, dict) else body
    report(
        "A2 fallback 沿用改寫後的內容",
        ok,
        {"status": response.status_code, "models": models, "sent": sent, "content": content},
    )


def check_tool_call(key: str) -> None:
    response = chat(key, "probe:tool=send_mail", user='to "SPIKE_SECRET"')
    arguments = response.json()["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
    report("A3 tool call 參數可還原", "SPIKE_SECRET" in arguments, arguments)


def check_streaming(key: str) -> None:
    clear()
    response = chat(key, "probe:reply=echo", "probe:split=3", user="ab SPIKE_SECRET", stream=True)
    sent = recorded()[-1]["messages"][-1]["content"]
    text = stream_text(response.text)
    report("A4 streaming 的請求也經過 pre_call 改寫", sent == "ab <SPIKE_1>", sent)
    report("A4 streaming chunk 可改寫（佔位符在同一 chunk）", text == "ab SPIKE_SECRET", text)
    # 切在佔位符中間：逐 chunk 取代無法還原，證明需要跨 chunk 暫存
    response = chat(key, "probe:reply=echo", "probe:split=6", user="ab SPIKE_SECRET", stream=True)
    text = stream_text(response.text)
    report("A4 佔位符跨 chunk 時逐 chunk 取代的結果", None, text)


def check_http_exception(key: str) -> None:
    response = chat(key, "probe:reply=echo", user="SPIKE_DENY")
    report("A5 HTTPException 的狀態碼與內容", response.status_code == 403, response.text[:300])
    response = chat(key, "probe:reply=echo", user="SPIKE_DENY", stream=True)
    report("A5 streaming 請求的 HTTPException", response.status_code == 403, response.text[:300])


def check_body_opt_out(key: str) -> None:
    clear()
    messages = [
        {"role": "system", "content": "probe:reply=echo"},
        {"role": "user", "content": "SPIKE_SECRET"},
    ]
    httpx.post(
        f"{LITELLM_URL}/v1/chat/completions",
        headers={"Authorization": f"Bearer {KEYS[key]}"},
        json={
            "model": "probe-primary",
            "messages": messages,
            "metadata": {
                "disable_global_guardrails": True,
                "opted_out_global_guardrails": ["m4-spike"],
            },
            "guardrails": [],
        },
        timeout=60,
    )
    sent = recorded()[-1]["messages"][-1]["content"]
    report("請求 body 無法關閉 default_on guardrail", sent == "<SPIKE_1>", sent)


def check_embeddings(key: str) -> None:
    response = httpx.post(
        f"{LITELLM_URL}/v1/embeddings",
        headers={"Authorization": f"Bearer {KEYS[key]}"},
        json={"model": "probe-primary", "input": "SPIKE_SECRET"},
        timeout=30,
    )
    report("A1 embeddings 的 call_type（看 gateway 日誌）", None, response.status_code)


def check_primary_only_key() -> None:
    generated = httpx.post(
        f"{LITELLM_URL}/key/generate",
        headers={"Authorization": f"Bearer {env('LITELLM_MASTER_KEY')}"},
        json={"key_alias": f"spike-primary-only-{int(time.time())}", "models": ["probe-primary"]},
        timeout=30,
    ).json()
    KEYS["spike-primary-only"] = generated["key"]
    clear()
    response = chat("spike-primary-only", "probe:reply=ok", "probe:fail=probe-primary", user="x")
    models = [r["model"] for r in recorded()]
    report(
        "A7 key 只列主力模型時 fallback 的結果",
        response.status_code == 200 and models[-1:] == ["probe-fallback"],
        {"status": response.status_code, "models": models, "body": response.text[:200]},
    )


def check_tpm() -> None:
    results = []
    for _ in range(3):
        clear()
        response = chat("guard-tpm", "probe:usage=5000", user="SPIKE_SECRET")
        results.append(
            {
                "status": response.status_code,
                "provider_models": [r["model"] for r in recorded()],
                "body": redact(response.text[:160]) if response.status_code != 200 else "",
            }
        )
    report("A8 tpm_limit=1000、每次 5000 token 的結果序列", None, results)


def main() -> None:
    key = "kyt-agent@llm-demo"
    check_rewrite_and_restore(key)
    check_tool_call(key)
    check_streaming(key)
    check_http_exception(key)
    check_body_opt_out(key)
    # 失敗的 deployment 會進入 cooldown（預設 5 秒），之後的請求直接走 fallback：每項之間等待
    check_primary_only_key()
    time.sleep(6)
    check_fallback(key)
    time.sleep(6)
    check_tpm()
    check_embeddings(key)


if __name__ == "__main__":
    main()
