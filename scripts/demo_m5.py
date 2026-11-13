"""依序執行 spec §13 的 M5 情境；全部使用 probe_llm，不花 LLM 費用。任一失敗時結束碼為 1。

低額度的測試帳號 erin（quota_probe 等級）在需要時清空帳本：只刪 governance 資料庫中
user_sub 為 erin 的列。
"""

import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx

from scripts.budget_sim import (
    QUOTA_USER,
    TZ,
    Row,
    call_id,
    clear_usage,
    governance,
    limit_audits,
    next_local_midnight,
    probe_cost,
    query,
    quota_tier,
    settled_rows,
    success_log,
    usage_rows,
    usage_turn,
    user_sub,
)
from scripts.demo_m2 import api, compose
from scripts.kc import client_token, user_token
from scripts.llm_sim import AGENT_KEY, chat, clear_probe, probe_requests

TIER = quota_tier()
DAY = TIER["budget_usd"]["day"]
results: list[bool] = []


def check(name: str, ok: bool, detail: object) -> None:
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


def send(prompt_tokens: int = 1, *directives: str, **kwargs: Any) -> httpx.Response:
    kwargs.setdefault("user", QUOTA_USER)
    return chat(AGENT_KEY, usage_turn(prompt_tokens, *directives), **kwargs)


def denied(response: httpx.Response, status: int, reason: str) -> bool:
    return response.status_code == status and governance(response).get("reasons") == [reason]


def fresh(sub: str) -> None:
    clear_usage(sub)
    clear_probe()


def show_overage(rows: list[Row]) -> None:
    """帳本每一筆的成本與超額；crossed 來自同一筆的 llm_limit_exceeded 稽核。"""
    audits = {
        a["details"]["call_id"]: a["details"] for a in limit_audits([r["call_id"] for r in rows])
    }
    for row in rows:
        crossed = audits.get(row["call_id"], {}).get("crossed", {}).get("budget_day", "-")
        print(
            f"      {row['call_id'][:8]}  cost_usd={row['cost_usd']}  "
            f"over_day_usd={row['over_day_usd']}  crossed={crossed}"
        )


def budget_denied(response: httpx.Response) -> bool:
    limit = governance(response).get("limit", {})
    return (
        denied(response, 403, "user_budget_exceeded")
        and limit.get("type") == "budget_day"
        and datetime.fromisoformat(limit["resets_at"]) == next_local_midnight(datetime.now(TZ))
    )


def matches_spend_log(row: Row, response: httpx.Response) -> tuple[bool, object]:
    log = success_log(call_id(response))
    if log is None:
        return False, "SpendLogs 60 秒內沒有紀錄"
    ok = row["cost_usd"] == Decimal(str(log["spend"])) and (
        row["prompt_tokens"],
        row["completion_tokens"],
    ) == (log["prompt_tokens"], log["completion_tokens"])
    return ok, {"ledger": str(row["cost_usd"]), "spend_logs": log["spend"], "model": log["model"]}


def identity_scenarios(sub: str) -> None:
    fresh(sub)
    check("1 缺使用者 token：401", denied(send(user=None), 401, "missing_user_token"), 401)
    check("2 無效 token：401", denied(send(token="not-a-jwt"), 401, "invalid_user_token"), 401)
    service = client_token("kyt-agent", "KYT_AGENT_CLIENT_SECRET")
    response = send(token=service)
    check("3 服務帳號 token：401", denied(response, 401, "invalid_user_token"), "azp=kyt-agent")
    response = send(body={"user": "mallory"})
    check(
        "4 body 自帶 user：400",
        denied(response, 400, "end_user_override") and governance(response)["field"] == "user",
        governance(response).get("field"),
    )
    response = send(headers={"x-litellm-customer-id": "mallory"})
    check(
        "5 自帶 end user header：400",
        denied(response, 400, "end_user_override"),
        governance(response).get("field"),
    )
    check("6 以上請求都沒有送到 provider", probe_requests() == [], len(probe_requests()))


def single_request_scenarios(sub: str) -> None:
    fresh(sub)
    cap = TIER["max_output_tokens"]
    response = send(body={"max_tokens": cap + 1})
    check("7 max_tokens 超過等級上限：400", denied(response, 400, "max_tokens_exceeded"), cap + 1)
    response = send(body={"n": 2})
    check("8 n 不是 1：400", denied(response, 400, "unsupported_field"), "n=2")
    response = send()
    injected = probe_requests()[-1].get("max_tokens") if probe_requests() else None
    check(
        "9 沒帶 max_tokens 時補上等級上限",
        response.status_code == 200 and injected == cap,
        f"provider 收到 max_tokens={injected}",
    )


def settlement_scenarios(sub: str) -> None:
    fresh(sub)
    response = send(123, stream=True)
    [row] = settled_rows(sub, 1)
    ok, detail = matches_spend_log(row, response)
    check(
        "10 streaming 結算：帳本與 LiteLLM_SpendLogs 一致",
        ok and row["settled_by"] == "callback" and row["cost_usd"] == probe_cost(123),
        detail,
    )
    fresh(sub)
    try:
        response = send(200, "probe:fail=probe-primary")
        rows = settled_rows(sub, 1)
        ok, detail = matches_spend_log(rows[0], response)
        check(
            "11 fallback 結算一次，以 fallback 的成本計",
            response.status_code == 200 and len(rows) == 1 and ok,
            detail,
        )
    finally:
        # 主力回 503 後 deployment 冷卻約 5 秒
        time.sleep(6)


def crossing_scenarios(sub: str) -> None:
    fresh(sub)
    send(1500)
    crossing = send(500)
    rows = settled_rows(sub, 2)
    total = sum((row["cost_usd"] for row in rows), Decimal(0))
    print(f"    日額度 {DAY}，已結算 {total}")
    show_overage(rows)
    audits = limit_audits([row["call_id"] for row in rows])
    check(
        "12 跨過日額度的那一筆放行，超額記在帳本與稽核",
        crossing.status_code == 200
        and rows[-1]["over_day_usd"] == total - DAY
        and len(audits) == 1
        and audits[0]["details"]["crossed"] == {"budget_day": True},
        f"over_day_usd={rows[-1]['over_day_usd']}",
    )
    response = send()
    check(
        "13 之後的請求 403，resets_at 為台北隔日 00:00",
        budget_denied(response),
        governance(response).get("limit"),
    )
    response = chat(AGENT_KEY, usage_turn(1), user="alice")
    check("14 其他使用者不受影響", response.status_code == 200, "alice → 200")

    fresh(sub)
    send(1700)
    token = user_token(QUOTA_USER)
    concurrent = TIER["max_concurrent"]
    # streaming 每個 chunk 前等待：三筆都在任何一筆結算前放行
    with ThreadPoolExecutor(concurrent) as pool:
        responses = list(
            pool.map(
                lambda _: send(200, "probe:chunk_delay=0.5", token=token, stream=True),
                range(concurrent),
            )
        )
    rows = settled_rows(sub, 1 + concurrent)
    total = sum((row["cost_usd"] for row in rows), Decimal(0))
    over = sum((row["over_day_usd"] for row in rows), Decimal(0))
    print(f"    日額度 {DAY}，並行 {concurrent} 筆後已結算 {total}")
    show_overage(rows)
    audits = limit_audits([row["call_id"] for row in rows])
    check(
        "15 並行跨過上限：超額加總等於總成本減日額度",
        all(r.status_code == 200 for r in responses)
        and over == total - DAY
        and len(audits) == sum(1 for row in rows if row["over_day_usd"] > 0),
        f"sum(over_day_usd)={over}",
    )


def rate_scenarios(sub: str) -> None:
    fresh(sub)
    token = user_token(QUOTA_USER)
    concurrent = TIER["max_concurrent"]
    with ThreadPoolExecutor(concurrent) as pool:
        slow = [
            pool.submit(send, 1, "probe:chunk_delay=1", token=token, stream=True)
            for _ in range(concurrent)
        ]
        time.sleep(1)
        extra = send(token=token)
        statuses = [future.result().status_code for future in slow]
    limit = governance(extra).get("limit", {})
    check(
        "16 同時進行數達上限：429",
        statuses == [200] * concurrent
        and denied(extra, 429, "user_rate_limited")
        and limit.get("type") == "concurrency",
        limit,
    )
    fresh(sub)
    statuses = [send().status_code for _ in range(TIER["rpm"])]
    response = send()
    limit = governance(response).get("limit", {})
    check(
        "17 個人 rpm 達上限：429 與 retry_after_seconds",
        statuses == [200] * TIER["rpm"]
        and denied(response, 429, "user_rate_limited")
        and limit.get("type") == "rpm"
        and 1 <= limit.get("retry_after_seconds", 0) <= 60,
        limit,
    )


def reconcile_scenario(sub: str) -> None:
    fresh(sub)
    response = send(321)
    [row] = settled_rows(sub, 1)
    if success_log(call_id(response)) is None:
        check("18 對帳補結算", False, "SpendLogs 60 秒內沒有紀錄")
        return
    # 模擬 callback 遺失：改回 in_flight 並清空結算欄位
    query(
        "governance",
        "UPDATE llm_usage SET status = 'in_flight', settled_at = NULL, settled_by = NULL, "
        "cost_usd = NULL, prompt_tokens = NULL, completion_tokens = NULL, over_day_usd = NULL, "
        "over_month_usd = NULL, over_tpm_tokens = NULL WHERE owner = %s AND call_id = %s",
        (row["owner"], row["call_id"]),
    )
    output = compose(
        "exec",
        "-T",
        "gateway",
        "python",
        "-m",
        "ai_gov_gateway.reconcile",
        "--older-than",
        "0s",
        "--release-missing-after",
        "1h",
    )
    [reconciled] = usage_rows(sub)
    ok, detail = matches_spend_log(reconciled, response)
    check(
        "18 對帳：遺失 callback 的請求依 SpendLogs 補結算",
        ok and reconciled["settled_by"] == "reconcile",
        output.splitlines()[0] if output else detail,
    )


def main() -> int:
    sub = user_sub(QUOTA_USER)
    identity_scenarios(sub)
    single_request_scenarios(sub)
    settlement_scenarios(sub)
    crossing_scenarios(sub)
    rate_scenarios(sub)
    reconcile_scenario(sub)

    verify = api("GET", "/audit/verify", "bob").json()
    check("19 稽核鏈完整", verify["first_invalid_id"] is None, verify)
    clear_usage(sub)

    print(f"\n{sum(results)}/{len(results)} 通過")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
