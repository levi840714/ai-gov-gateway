"""依序觀察 B1–B6、B9、B10，印出 PASS / FAIL / INFO 與細節；結論寫在 docs/spikes/m5-findings.md。

前提：gateway 以 docker-compose.spike-m5.yml 疊加啟動。
輸出只含數字、布林與雜湊前綴，不含 key 或 token。
"""

import hashlib
import json
import subprocess
import time
from decimal import Decimal
from typing import Any

import httpx
import psycopg

from scripts.env import KEYS_FILE, LITELLM_URL, env
from scripts.kc import user_token
from scripts.make_keys import LLM_ROUTES

PROBE = "http://127.0.0.1:8300"
GATEWAY_CONTAINER = "ai-gov-gateway-gateway-1"
KEYS: dict[str, str] = json.loads(KEYS_FILE.read_text())
AGENT_KEY = "kyt-agent@1.5"
# 與 config.spike-m5.yaml 的 probe 單價相同
INPUT_PRICE = Decimal("0.000001")
OUTPUT_PRICE = Decimal("0.00001")


def report(name: str, ok: bool | None, detail: object) -> None:
    label = "INFO" if ok is None else "PASS" if ok else "FAIL"
    print(f"{label}  {name}  {detail}", flush=True)


def headers(key: str, token: str | None) -> dict[str, str]:
    result = {"x-litellm-api-key": f"Bearer {key}"}
    if token:
        result["Authorization"] = f"Bearer {token}"
    return result


def body(*system: str, user: str = "hello", stream: bool = False, model: str = "probe-primary"):
    messages = [{"role": "system", "content": line} for line in system]
    messages.append({"role": "user", "content": user})
    return {"model": model, "messages": messages, "stream": stream}


def chat(key: str, token: str | None, payload: dict[str, Any]) -> httpx.Response:
    return httpx.post(
        f"{LITELLM_URL}/v1/chat/completions",
        headers=headers(key, token),
        json=payload,
        timeout=60,
    )


def call_id_of(response: httpx.Response) -> str | None:
    return response.headers.get("x-litellm-call-id")


def events(since: float) -> list[tuple[str, dict[str, Any]]]:
    logs = subprocess.run(
        ["docker", "logs", GATEWAY_CONTAINER, "--since", str(int(since) - 1)],
        capture_output=True,
        text=True,
        check=True,
    )
    found = []
    for line in (logs.stdout + logs.stderr).splitlines():
        if "M5SPIKE " not in line:
            continue
        tag, payload = line.split("M5SPIKE ", 1)[1].split(" ", 1)
        found.append((tag, json.loads(payload)))
    return sorted(found, key=lambda item: item[1]["seq"])


def of_call(found: list[tuple[str, dict[str, Any]]], call_id: str | None) -> list[str]:
    return [tag for tag, payload in found if payload.get("call_id") == call_id]


def first(found, tag: str, call_id: str | None) -> dict[str, Any]:
    return next((p for t, p in found if t == tag and p.get("call_id") == call_id), {})


def recorded() -> list[dict[str, Any]]:
    return httpx.get(f"{PROBE}/requests").json()


def recorded_headers() -> list[dict[str, Any]]:
    return httpx.get(f"{PROBE}/request-headers").json()


def clear_probe() -> None:
    httpx.delete(f"{PROBE}/requests")


def master(path: str, payload: dict[str, Any]) -> httpx.Response:
    return httpx.post(
        f"{LITELLM_URL}{path}",
        headers={"Authorization": f"Bearer {env('LITELLM_MASTER_KEY')}"},
        json=payload,
        timeout=30,
    )


def short_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:8]


def expected_cost(success: dict[str, Any]) -> Decimal:
    return (
        Decimal(success.get("prompt_tokens") or 0) * INPUT_PRICE
        + Decimal(success.get("completion_tokens") or 0) * OUTPUT_PRICE
    )


def check_basic(token: str) -> dict[str, dict[str, Any]]:
    """B1、B2、B3（非 streaming 與 streaming）、B10。回傳 call_id 對應的 success 事件。"""
    successes: dict[str, dict[str, Any]] = {}
    for stream in (False, True):
        clear_probe()
        started = time.time()
        response = chat(KEYS[AGENT_KEY], token, body("probe:reply=echo", stream=stream))
        call_id = call_id_of(response)
        time.sleep(2)
        found = events(started)
        label = "streaming" if stream else "非 streaming"
        pre = first(found, "pre_call", call_id)
        report(
            f"{label}：狀態碼與 call_id",
            response.status_code == 200 and bool(call_id),
            response.status_code,
        )
        report(
            f"B1 {label}：raw headers 含 authorization 且為 JWT",
            "authorization" in pre.get("raw_header_names", [])
            and pre.get("authorization_is_jwt") is True,
            {k: pre.get(k) for k in ("secret_fields_type", "authorization_is_jwt")},
        )
        report(f"B1 {label}：raw header 名稱", None, pre.get("raw_header_names"))
        sent_headers = recorded_headers()[-1]
        report(
            f"B2 {label}：provider 沒有收到 JWT",
            sent_headers["authorization"] != "jwt" and not sent_headers["jwt_headers"],
            {
                "authorization": sent_headers["authorization"],
                "jwt_headers": sent_headers["jwt_headers"],
            },
        )
        report(
            f"B10 {label}：provider 收到改寫的 max_tokens",
            recorded()[-1].get("max_tokens") == 17,
            recorded()[-1].get("max_tokens"),
        )
        tags = of_call(found, call_id)
        success = first(found, "log_success", call_id)
        report(f"B3 {label}：事件順序", tags.count("log_success") == 1, tags)
        report(
            f"B3 {label}：成本等於 token × 單價",
            success.get("response_cost") is not None
            and Decimal(str(success["response_cost"])) == expected_cost(success),
            {
                k: success.get(k)
                for k in (
                    "response_cost",
                    "slo_response_cost",
                    "prompt_tokens",
                    "completion_tokens",
                )
            },
        )
        report(
            f"B3 {label}：success 事件的 key 雜湊與 pre-call 的 user_api_key_dict.token 同源",
            bool(pre.get("key_token_hash"))
            and success.get("key_hash_hash") == pre.get("key_token_hash"),
            {"pre": pre.get("key_token_hash"), "success": success.get("key_hash_hash")},
        )
        report(
            f"B3 {label}：standard_logging_object 的 id 等於 call_id",
            success.get("slo_id") == call_id,
            success.get("slo_id") == call_id,
        )
        if call_id:
            successes[call_id] = success
    report("B3 standard_logging_object 的鍵", None, success.get("slo_keys"))
    return successes


def check_fallback(token: str) -> dict[str, dict[str, Any]]:
    """B3（fallback）、B4、B10（fallback）。"""
    clear_probe()
    started = time.time()
    response = chat(KEYS[AGENT_KEY], token, body("probe:reply=echo", "probe:fail=probe-primary"))
    call_id = call_id_of(response)
    time.sleep(2)
    found = events(started)
    tags = of_call(found, call_id)
    success = first(found, "log_success", call_id)
    sent = [(r.get("model"), r.get("max_tokens")) for r in recorded()]
    report("fallback：狀態碼", response.status_code == 200, response.status_code)
    report(
        "B3 fallback：只有一次 success，模型為 fallback",
        tags.count("log_success") == 1 and "fallback" in str(success.get("model")),
        {"tags": tags, "model": success.get("model"), "model_group": success.get("model_group")},
    )
    report("B4 fallback：log_failure 次數（主力的每次失敗）", None, tags.count("log_failure"))
    report(
        "B4 fallback：failure hook 次數",
        tags.count("failure_hook") == 0,
        tags.count("failure_hook"),
    )
    report("B10 fallback：provider 收到的 (model, max_tokens)", all(m == 17 for _, m in sent), sent)
    report(
        "B3 fallback：成本等於 fallback 的 token × 單價",
        success.get("response_cost") is not None
        and Decimal(str(success["response_cost"])) == expected_cost(success),
        success.get("response_cost"),
    )
    # 主力回 503 後約 5 秒冷卻（M4 findings）
    time.sleep(6)
    return {call_id: success} if call_id else {}


def temp_key(alias: str, **extra: Any) -> str:
    response = master(
        "/key/generate", {"key_alias": alias, "models": ["probe-primary"], **LLM_ROUTES, **extra}
    )
    response.raise_for_status()
    return str(response.json()["key"])


def delete_key(key: str) -> None:
    master("/key/delete", {"keys": [key]})


def check_rate_limit_fallback(token: str) -> None:
    """B5：tpm 超限觸發的 fallback 以新 call_id 重跑 pre-call，之後收到哪些事件。"""
    key = temp_key(f"m5-spike-tpm-{int(time.time())}", tpm_limit=1)
    try:
        first_response = chat(key, token, body())
        time.sleep(2)
        started = time.time()
        response = chat(key, token, body())
        time.sleep(3)
        found = events(started)
        pre_ids = [p["call_id"] for t, p in found if t == "pre_call"]
        detail = {
            "first": first_response.status_code,
            "second": response.status_code,
            "pre_call_ids": len(pre_ids),
            "events": {cid[-6:]: of_call(found, cid) for cid in pre_ids},
            "response_call_id_is_first": call_id_of(response) == (pre_ids[0] if pre_ids else None),
        }
        later = pre_ids[1:]
        report(
            "B5 第二個 call_id 收不到 success / failure 事件",
            response.status_code == 429
            and len(pre_ids) >= 2
            and all(
                not ({"log_success", "log_failure", "failure_hook"} & set(of_call(found, cid)))
                for cid in later
            ),
            detail,
        )
    finally:
        delete_key(key)


def check_stream_disconnect(token: str) -> None:
    """B6：streaming 讀到第一個 chunk 後中斷。"""
    started = time.time()
    payload = body("probe:reply=echo", "probe:chunk_delay=1", user="abcdefghij" * 5, stream=True)
    with httpx.stream(
        "POST",
        f"{LITELLM_URL}/v1/chat/completions",
        headers=headers(KEYS[AGENT_KEY], token),
        json=payload,
        timeout=60,
    ) as response:
        call_id = call_id_of(response)
        for line in response.iter_lines():
            if line.startswith("data: "):
                break
    time.sleep(8)
    found = events(started)
    success = first(found, "log_success", call_id)
    report(
        "B6 中斷後的事件",
        None,
        {
            "tags": of_call(found, call_id),
            "chunks": first(found, "stream_end", call_id).get("chunks"),
            "cost": success.get("response_cost"),
            "completion_tokens": success.get("completion_tokens"),
        },
    )


def check_budget_duration() -> None:
    """B9：key 接受 budget_duration "1mo"。"""
    key = temp_key(f"m5-spike-duration-{int(time.time())}", max_budget=1.0, budget_duration="1mo")
    try:
        info = httpx.get(
            f"{LITELLM_URL}/key/info",
            params={"key": key},
            headers={"Authorization": f"Bearer {env('LITELLM_MASTER_KEY')}"},
            timeout=30,
        ).json()
        data = info.get("info") or {}
        report(
            "B9 budget_duration=1mo",
            data.get("budget_duration") == "1mo" and bool(data.get("budget_reset_at")),
            {
                "budget_duration": data.get("budget_duration"),
                "budget_reset_at": data.get("budget_reset_at"),
            },
        )
    finally:
        delete_key(key)


def check_spend_logs(successes: dict[str, dict[str, Any]]) -> None:
    """B3：success 事件的成本與 token 等於 LiteLLM_SpendLogs；api_key 欄位與事件的 key 雜湊同源。"""
    url = f"postgresql://litellm:{env('POSTGRES_PASSWORD')}@127.0.0.1:55433/litellm"
    rows: dict[str, tuple[Any, ...]] = {}
    for _ in range(18):
        with psycopg.connect(url) as conn:
            for row in conn.execute(
                "SELECT litellm_call_id, spend, prompt_tokens, completion_tokens, api_key, status "
                'FROM "LiteLLM_SpendLogs" WHERE litellm_call_id = ANY(%s)',
                (list(successes),),
            ):
                rows[row[0]] = row
        if len(rows) == len(successes):
            break
        time.sleep(5)
    for call_id, success in successes.items():
        row = rows.get(call_id)
        if row is None:
            report(f"B3 SpendLogs {call_id[-6:]}", False, "找不到紀錄")
            continue
        _, spend, prompt, completion, api_key, status = row
        ok = (
            Decimal(str(spend)) == Decimal(str(success.get("response_cost")))
            and prompt == success.get("prompt_tokens")
            and completion == success.get("completion_tokens")
        )
        report(
            f"B3 SpendLogs {call_id[-6:]} 成本與 token 一致",
            ok,
            {"spend": spend, "prompt": prompt, "completion": completion, "status": status},
        )
        report(
            f"B3 SpendLogs {call_id[-6:]} api_key 與事件同源",
            short_hash(api_key or "") == success.get("key_hash_hash"),
            short_hash(api_key or ""),
        )


def main() -> None:
    token = user_token("alice")
    successes = check_basic(token)
    successes |= check_fallback(token)
    check_rate_limit_fallback(user_token("alice"))
    check_stream_disconnect(user_token("alice"))
    check_budget_duration()
    check_spend_logs(successes)


if __name__ == "__main__":
    main()
