"""M5 的 demo 與整合測試共用：低額度測試帳號 erin 的帳本、LiteLLM_SpendLogs 與拒絕內容。"""

import json
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from datetime import time as clock_time
from decimal import Decimal
from typing import Any, LiteralString
from zoneinfo import ZoneInfo

import httpx
import jwt
import psycopg
import yaml
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

from scripts.env import ROOT, env
from scripts.kc import user_token

QUOTA_USER = "erin"
# gateway 的 GOV_BUDGET_TIMEZONE 預設值；日額度在台北時間 00:00 重置
TZ = ZoneInfo("Asia/Taipei")
# probe_llm 的回應一律 10 個輸出 token
PROBE_COMPLETION_TOKENS = 10
Row = dict[str, Any]


def _probe_prices() -> tuple[Decimal, Decimal]:
    config = yaml.safe_load((ROOT / "litellm" / "config.yaml").read_text(encoding="utf-8"))
    info = next(m["model_info"] for m in config["model_list"] if m["model_name"] == "probe-primary")
    return Decimal(str(info["input_cost_per_token"])), Decimal(str(info["output_cost_per_token"]))


INPUT_PRICE, OUTPUT_PRICE = _probe_prices()


def quota_tier() -> dict[str, Any]:
    """erin 的等級（policies/agents/data.json 的 quota_probe）；金額轉成 Decimal。"""
    agents = json.loads((ROOT / "policies" / "agents" / "data.json").read_text(encoding="utf-8"))
    tier = next(t for t in agents["kyt-agent"]["llm_limits"] if t["tier"] == "quota_probe")
    budgets = {period: Decimal(str(value)) for period, value in tier["budget_usd"].items()}
    return {**tier, "budget_usd": budgets}


def probe_cost(prompt_tokens: int) -> Decimal:
    return prompt_tokens * INPUT_PRICE + PROBE_COMPLETION_TOKENS * OUTPUT_PRICE


def usage_turn(prompt_tokens: int, *directives: str) -> list[dict[str, Any]]:
    """probe 回報的輸入 token 為 prompt_tokens，成本即 probe_cost(prompt_tokens)。"""
    system = "\n".join([f"probe:usage={prompt_tokens}", *directives])
    return [{"role": "system", "content": system}, {"role": "user", "content": "x"}]


def user_sub(username: str) -> str:
    # 只讀 sub，不驗證簽章：token 剛由 Keycloak 簽發
    claims = jwt.decode(user_token(username), options={"verify_signature": False})
    return str(claims["sub"])


def database_url(name: str) -> str:
    """與 GOV_TEST_DATABASE_URL 同一個 Postgres，換成指定的資料庫。"""
    params = conninfo_to_dict(env("GOV_TEST_DATABASE_URL"))
    params["dbname"] = name
    return make_conninfo(**params)


def query(database: str, sql: LiteralString, params: tuple[Any, ...] = ()) -> list[Row]:
    with psycopg.connect(database_url(database), row_factory=dict_row, autocommit=True) as conn:
        cursor = conn.execute(sql, params)
        return cursor.fetchall() if cursor.description else []


def clear_usage(sub: str) -> None:
    """只刪這位使用者的帳本；帳本在 gateway 使用的 governance 資料庫。"""
    query("governance", "DELETE FROM llm_usage WHERE user_sub = %s", (sub,))


def usage_rows(sub: str) -> list[Row]:
    return query(
        "governance",
        "SELECT * FROM llm_usage WHERE user_sub = %s ORDER BY settled_at NULLS LAST, created_at",
        (sub,),
    )


def wait_for(
    read: Callable[[], list[Row]], ready: Callable[[list[Row]], bool], timeout: float
) -> list[Row]:
    """結算在回應之後才完成，SpendLogs 也是批次寫入：輪詢到條件成立或逾時。"""
    deadline = time.monotonic() + timeout
    while True:
        rows = read()
        if ready(rows) or time.monotonic() > deadline:
            return rows
        time.sleep(0.5)


def settled_rows(sub: str, count: int, timeout: float = 15) -> list[Row]:
    def done(rows: list[Row]) -> bool:
        return len(rows) >= count and all(row["status"] != "in_flight" for row in rows)

    return wait_for(lambda: usage_rows(sub), done, timeout)


def spend_logs(call_ids: list[str]) -> list[Row]:
    return query(
        "litellm",
        "SELECT litellm_call_id, status, spend, prompt_tokens, completion_tokens, model "
        'FROM "LiteLLM_SpendLogs" WHERE litellm_call_id = ANY(%s)',
        (call_ids,),
    )


def success_log(call_id: str, timeout: float = 60) -> Row | None:
    rows = wait_for(
        lambda: [r for r in spend_logs([call_id]) if r["status"] == "success"], bool, timeout
    )
    return rows[0] if rows else None


def limit_audits(call_ids: list[str]) -> list[Row]:
    return query(
        "governance",
        "SELECT reasons, details FROM audit_events WHERE event_type = 'llm_limit_exceeded' "
        "AND details->>'call_id' = ANY(%s) ORDER BY id",
        (call_ids,),
    )


def access_denials(sub: str, reason: str) -> int:
    rows = query(
        "governance",
        "SELECT count(*) AS n FROM audit_events WHERE event_type = 'llm_access' "
        "AND user_sub = %s AND reasons::text LIKE %s",
        (sub, f'%"{reason}"%'),
    )
    return int(rows[0]["n"])


def governance(response: httpx.Response) -> dict[str, Any]:
    """拒絕時 gateway 回傳的 governance JSON（error.provider_specific_fields）。"""
    try:
        return dict(response.json()["error"]["provider_specific_fields"])
    except (ValueError, KeyError, TypeError):
        return {}


def call_id(response: httpx.Response) -> str:
    return response.headers["x-litellm-call-id"]


def next_local_midnight(now: datetime) -> datetime:
    tomorrow: date = now.astimezone(TZ).date() + timedelta(days=1)
    return datetime.combine(tomorrow, clock_time(), tzinfo=TZ)
