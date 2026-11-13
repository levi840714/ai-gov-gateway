"""M5 依使用者的 LLM 限制：gateway + probe_llm + Keycloak 的契約測試（spec §13）。

低額度的測試帳號 erin（quota_probe 等級）在每個測試前清空帳本。帳本在 gateway 使用的
governance 資料庫（不是 _test 結尾），所以只刪 user_sub 為 erin 的列；rpm 也由帳本計算，清空後歸零。
成本以 probe:usage 控制，預期值由 litellm/config.yaml 的 probe 單價算出。
"""

import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest

from scripts.budget_sim import (
    PROBE_COMPLETION_TOKENS,
    QUOTA_USER,
    TZ,
    access_denials,
    call_id,
    clear_usage,
    governance,
    limit_audits,
    next_local_midnight,
    probe_cost,
    query,
    quota_tier,
    settled_rows,
    spend_logs,
    success_log,
    usage_rows,
    usage_turn,
    user_sub,
)
from scripts.env import LITELLM_URL, ROOT
from scripts.kc import client_token, user_token
from scripts.llm_sim import AGENT_KEY, PROBE_URL, chat, clear_probe, probe_requests

pytestmark = pytest.mark.integration
TIER = quota_tier()
DAY = TIER["budget_usd"]["day"]
# 主力回 503 後 deployment 冷卻約 5 秒（M4 findings A2）
COOLDOWN = 6


@pytest.fixture(scope="module")
def erin() -> str:
    # 帳本與 SpendLogs 的連線沿用 GOV_TEST_DATABASE_URL 的主機與帳號
    if not os.environ.get("GOV_TEST_DATABASE_URL"):
        pytest.skip("GOV_TEST_DATABASE_URL not set")
    try:
        httpx.get(f"{LITELLM_URL}/health/liveliness", timeout=3).raise_for_status()
        httpx.get(f"{PROBE_URL}/health", timeout=3).raise_for_status()
        return user_sub(QUOTA_USER)
    except httpx.HTTPError:
        pytest.skip("gateway, probe_llm or keycloak is not running")


@pytest.fixture(autouse=True)
def clean(erin: str) -> None:
    clear_usage(erin)
    clear_probe()


def send(prompt_tokens: int = 1, *directives: str, **kwargs: Any) -> httpx.Response:
    kwargs.setdefault("user", QUOTA_USER)
    return chat(AGENT_KEY, usage_turn(prompt_tokens, *directives), **kwargs)


def reach_near_day_limit(erin: str, prompt_tokens: int) -> Decimal:
    response = send(prompt_tokens)
    assert response.status_code == 200, response.text
    rows = settled_rows(erin, 1)
    assert rows[0]["cost_usd"] == probe_cost(prompt_tokens) < DAY
    return probe_cost(prompt_tokens)


def assert_budget_denied(response: httpx.Response) -> None:
    body = governance(response)
    assert response.status_code == 403, response.text
    assert body["reasons"] == ["user_budget_exceeded"]
    assert body["limit"]["type"] == "budget_day"
    expected = next_local_midnight(datetime.now(TZ))
    assert datetime.fromisoformat(body["limit"]["resets_at"]) == expected


def test_raw_authorization_reaches_hook(erin):
    assert send().status_code == 200
    missing = send(user=None)
    assert missing.status_code == 401
    assert governance(missing)["reasons"] == ["missing_user_token"]
    invalid = send(token="not-a-jwt")
    assert invalid.status_code == 401
    assert governance(invalid)["reasons"] == ["invalid_user_token"]


def test_jwt_not_forwarded(erin):
    assert send().status_code == 200
    headers = httpx.get(f"{PROBE_URL}/request-headers", timeout=10).json()[-1]
    assert headers["authorization"] != "jwt"
    assert headers["jwt_headers"] == []


def test_service_account_token_rejected(erin):
    token = client_token("kyt-agent", "KYT_AGENT_CLIENT_SECRET")
    response = send(token=token)
    assert response.status_code == 401
    assert governance(response)["reasons"] == ["invalid_user_token"]
    assert probe_requests() == []


END_USER_OVERRIDES = [
    ({"user": "mallory"}, {}, "user"),
    ({"metadata": {"user_id": "mallory"}}, {}, "metadata.user_id"),
    ({"metadata": {"user": "mallory"}}, {}, "metadata.user"),
    ({}, {"x-litellm-customer-id": "mallory"}, "x-litellm-customer-id"),
    ({}, {"x-litellm-end-user-id": "mallory"}, "x-litellm-end-user-id"),
]


def test_end_user_overrides_rejected(erin):
    rejected = []
    for body, headers, field in END_USER_OVERRIDES:
        response = send(body=body, headers=headers)
        assert response.status_code == 400, (field, response.text)
        assert governance(response)["reasons"] == ["end_user_override"]
        assert governance(response)["field"] == field
        rejected.append(call_id(response))
    assert probe_requests() == []
    assert usage_rows(erin) == []
    # LiteLLM 仍記 failure（spend 0）；確認沒有成功或計費的紀錄。SpendLogs 批次寫入，
    # 先等之後一筆成功的請求出現，確定前面的請求也已寫入
    marker = send()
    assert success_log(call_id(marker)) is not None
    logs = spend_logs(rejected)
    assert all(log["status"] != "success" and log["spend"] == 0 for log in logs)


def test_max_tokens_injected(erin):
    assert send().status_code == 200
    assert probe_requests()[-1]["max_tokens"] == TIER["max_output_tokens"]
    clear_probe()
    try:
        assert send(1, "probe:fail=probe-primary").status_code == 200
        sent = probe_requests()
        assert [r["model"] for r in sent] == ["probe-primary", "probe-primary", "probe-fallback"]
        assert all(r["max_tokens"] == TIER["max_output_tokens"] for r in sent)
    finally:
        time.sleep(COOLDOWN)


def test_single_request_caps(erin):
    cap = TIER["max_output_tokens"]
    cases = [
        ({"max_tokens": cap + 1}, "max_tokens_exceeded", "max_tokens"),
        ({"max_completion_tokens": cap + 1}, "max_tokens_exceeded", "max_completion_tokens"),
        ({"n": 2}, "unsupported_field", "n"),
    ]
    for body, code, field in cases:
        response = send(body=body)
        assert response.status_code == 400, (body, response.text)
        assert governance(response)["reasons"] == [code]
        assert governance(response)["field"] == field
    messages = [
        {"role": "system", "content": "probe:reply=ok"},
        {"role": "user", "content": "x" * (TIER["max_input_chars"] + 1)},
    ]
    response = chat(AGENT_KEY, messages, user=QUOTA_USER)
    assert response.status_code == 400
    assert governance(response)["reasons"] == ["content_too_large"]
    assert probe_requests() == []
    assert usage_rows(erin) == []
    assert send(body={"max_tokens": cap}).status_code == 200


@pytest.mark.parametrize("stream", [False, True])
def test_settlement_matches_spend_logs(erin, stream):
    response = send(123, stream=stream)
    assert response.status_code == 200, response.text
    [row] = settled_rows(erin, 1)
    log = success_log(call_id(response))
    assert log is not None, "LiteLLM_SpendLogs did not record the request within 60s"
    assert row["call_id"] == log["litellm_call_id"]
    assert row["status"] == "settled" and row["settled_by"] == "callback"
    assert row["cost_usd"] == Decimal(str(log["spend"])) == probe_cost(123)
    assert (row["prompt_tokens"], row["completion_tokens"]) == (
        log["prompt_tokens"],
        log["completion_tokens"],
    )


def test_fallback_settles_once(erin):
    try:
        response = send(200, "probe:fail=probe-primary")
        assert response.status_code == 200, response.text
        [row] = settled_rows(erin, 1)
        log = success_log(call_id(response))
        assert log is not None
        assert row["status"] == "settled" and row["model"] == "probe-primary"
        assert log["model"] == "openai/probe-fallback"
        assert row["cost_usd"] == Decimal(str(log["spend"])) == probe_cost(200)
    finally:
        time.sleep(COOLDOWN)


def test_crossing_request_records_overage(erin):
    first = reach_near_day_limit(erin, 1500)
    crossing = send(500)
    assert crossing.status_code == 200, crossing.text
    rows = settled_rows(erin, 2)
    total = first + probe_cost(500)
    assert total > DAY
    last = rows[-1]
    assert last["call_id"] == call_id(crossing)
    assert last["over_day_usd"] == total - DAY
    assert rows[0]["over_day_usd"] == 0
    [audit] = limit_audits([row["call_id"] for row in rows])
    assert audit["reasons"] == ["budget_day"]
    assert audit["details"]["call_id"] == call_id(crossing)
    assert audit["details"]["crossed"] == {"budget_day": True}
    assert Decimal(audit["details"]["over"]["budget_day"]["over"]) == total - DAY

    denials = access_denials(erin, "user_budget_exceeded")
    assert_budget_denied(send())
    assert access_denials(erin, "user_budget_exceeded") == denials + 1


def test_concurrent_crossing_overage_sums_exactly(erin):
    reach_near_day_limit(erin, 1700)
    token = user_token(QUOTA_USER)
    # streaming 每個 chunk 前等待：三筆都在任何一筆結算前放行
    concurrent = TIER["max_concurrent"]
    with ThreadPoolExecutor(concurrent) as pool:
        responses = list(
            pool.map(
                lambda _: send(200, "probe:chunk_delay=0.5", token=token, stream=True),
                range(concurrent),
            )
        )
    assert [r.status_code for r in responses] == [200] * concurrent
    rows = settled_rows(erin, 1 + concurrent)
    assert all(row["status"] == "settled" for row in rows)
    total = sum((row["cost_usd"] for row in rows), Decimal(0))
    assert total == probe_cost(1700) + concurrent * probe_cost(200)
    assert sum((row["over_day_usd"] for row in rows), Decimal(0)) == total - DAY

    over = {row["call_id"]: row for row in rows if row["over_day_usd"] > 0}
    audits = {a["details"]["call_id"]: a["details"] for a in limit_audits(list(over))}
    assert len(audits) == len(over) == concurrent
    # 只有一筆跨過上限、超額只是一部分；之後結算的並行請求整筆都是超額
    [crossing] = [cid for cid, details in audits.items() if details["crossed"]["budget_day"]]
    for cid, row in over.items():
        assert Decimal(audits[cid]["over"]["budget_day"]["over"]) == row["over_day_usd"]
        assert (row["over_day_usd"] < row["cost_usd"]) == (cid == crossing)
    assert_budget_denied(send())


def test_other_user_unaffected(erin):
    reach_near_day_limit(erin, 1500)
    assert send(1000).status_code == 200
    settled_rows(erin, 2)
    assert_budget_denied(send())
    assert send(user="alice").status_code == 200


def test_rpm_per_user(erin):
    statuses = [send().status_code for _ in range(TIER["rpm"])]
    assert statuses == [200] * TIER["rpm"]
    response = send()
    body = governance(response)
    assert response.status_code == 429, response.text
    assert body["reasons"] == ["user_rate_limited"]
    assert body["limit"]["type"] == "rpm"
    assert 1 <= body["limit"]["retry_after_seconds"] <= 60


def test_concurrency_limit(erin):
    token = user_token(QUOTA_USER)
    concurrent = TIER["max_concurrent"]
    with ThreadPoolExecutor(concurrent) as pool:
        slow = [
            pool.submit(send, 1, "probe:chunk_delay=1", token=token, stream=True)
            for _ in range(concurrent)
        ]
        # 等三筆都放行後，第四筆才會看到同時進行數已滿
        time.sleep(1)
        extra = send(token=token)
        statuses = [future.result().status_code for future in slow]
    body = governance(extra)
    assert statuses == [200] * concurrent
    assert extra.status_code == 429, extra.text
    assert body["reasons"] == ["user_rate_limited"]
    assert body["limit"]["type"] == "concurrency"


def test_rejected_request_releases_row(erin):
    # probe-fallback 沒有下一層 fallback：放行後 provider 一律失敗，由 failure hook 釋放
    try:
        response = send(1, "probe:fail=probe-fallback", model="probe-fallback")
        assert response.status_code >= 500, response.text
        [row] = settled_rows(erin, 1)
        assert row["status"] == "released"
        assert row["cost_usd"] is None
    finally:
        time.sleep(COOLDOWN)


def test_reconcile_settles_missing_callback(erin):
    response = send(321)
    assert response.status_code == 200, response.text
    [row] = settled_rows(erin, 1)
    log = success_log(call_id(response))
    assert log is not None
    # 模擬 callback 遺失：改回 in_flight 並清空結算欄位
    query(
        "governance",
        "UPDATE llm_usage SET status = 'in_flight', settled_at = NULL, settled_by = NULL, "
        "cost_usd = NULL, prompt_tokens = NULL, completion_tokens = NULL, over_day_usd = NULL, "
        "over_month_usd = NULL, over_tpm_tokens = NULL WHERE owner = %s AND call_id = %s",
        (row["owner"], row["call_id"]),
    )
    done = subprocess.run(
        [
            "docker",
            "compose",
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
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "settled=" in done.stdout
    [reconciled] = usage_rows(erin)
    assert reconciled["status"] == "settled" and reconciled["settled_by"] == "reconcile"
    assert reconciled["cost_usd"] == Decimal(str(log["spend"])) == probe_cost(321)
    assert (reconciled["prompt_tokens"], reconciled["completion_tokens"]) == (
        321,
        PROBE_COMPLETION_TOKENS,
    )
