from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Json

from ai_gov_gateway.engine.audit import GENESIS_HASH, build_record
from ai_gov_gateway.engine.ledger import (
    Admission,
    AuditBuilder,
    DuplicateCallId,
    LedgerUnavailable,
    SettledRow,
    Settlement,
)
from ai_gov_gateway.engine.limits import (
    AdmissionSnapshot,
    LimitDenial,
    Overage,
    SettleContext,
    UsageLimits,
    compute_overage,
    period_bounds,
)
from ai_gov_gateway.engine.models import (
    AgentRef,
    Approval,
    ApprovalStatus,
    AuditEvent,
    AuditRecord,
    UserIdentity,
)
from ai_gov_gateway.engine.store import (
    ApprovalConflict,
    StoreError,
    StoreUnavailable,
    transition_columns,
)

# 固定的 advisory lock key，讓多個 worker 寫入稽核時 hash chain 維持單一序列
AUDIT_LOCK_KEY = 72_410_001
# 與 AUDIT_LOCK_KEY 不同的命名空間：雙參數的 advisory lock，第二個參數是（agent, 使用者）
USAGE_LOCK_SPACE = 72_410_002
ONE_OPEN_INDEX = "approvals_one_open"
CONNECT_TIMEOUT_SECONDS = 5

# arguments、reasons、details 與 limits 用 json 而非 jsonb：jsonb 會改寫數字（1e22、-0.0），
# 讀回後的 hash 就對不上。
SCHEMA = """
CREATE TABLE IF NOT EXISTS approvals (
    id text PRIMARY KEY,
    fingerprint text NOT NULL,
    status text NOT NULL,
    agent_id text NOT NULL,
    agent_version text NOT NULL,
    key_alias text NOT NULL,
    user_sub text NOT NULL,
    username text NOT NULL,
    server text NOT NULL,
    tool text NOT NULL,
    arguments json NOT NULL,
    reasons json NOT NULL,
    approver_role text NOT NULL,
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    decided_by text,
    decided_by_username text,
    decided_at timestamptz,
    comment text,
    used_at timestamptz
);
CREATE INDEX IF NOT EXISTS approvals_fingerprint ON approvals (fingerprint, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS approvals_one_open ON approvals (fingerprint)
    WHERE status IN ('pending', 'approved');
CREATE TABLE IF NOT EXISTS audit_events (
    id bigint PRIMARY KEY,
    ts timestamptz NOT NULL,
    event_type text NOT NULL,
    agent_id text,
    agent_version text,
    key_alias text,
    user_sub text,
    username text,
    actor_sub text,
    actor_username text,
    server text,
    tool text,
    arguments json,
    decision text,
    reasons json NOT NULL,
    approval_id text,
    policy_version text,
    comment text,
    details json,
    prev_hash text NOT NULL,
    hash text NOT NULL
);
ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS details json;
CREATE TABLE IF NOT EXISTS llm_usage (
    owner text NOT NULL,
    call_id text NOT NULL,
    agent_id text NOT NULL,
    user_sub text NOT NULL,
    model text NOT NULL,
    tier text NOT NULL,
    limits json NOT NULL,
    status text NOT NULL,
    created_at timestamptz NOT NULL,
    settled_at timestamptz,
    settled_by text,
    cost_usd numeric,
    prompt_tokens integer,
    completion_tokens integer,
    over_day_usd numeric,
    over_month_usd numeric,
    over_tpm_tokens integer,
    PRIMARY KEY (owner, call_id)
);
CREATE INDEX IF NOT EXISTS llm_usage_user ON llm_usage (agent_id, user_sub, created_at);
CREATE INDEX IF NOT EXISTS llm_usage_settled ON llm_usage (agent_id, user_sub, settled_at);
"""

# 每月 1 日的頭一分鐘，60 秒視窗會早於 month_start，所以範圍取三者中最早的
ADMISSION_SNAPSHOT = """
SELECT
  coalesce(sum(cost_usd) FILTER (WHERE status = 'settled' AND created_at >= %(day_start)s), 0)
    AS day_usd,
  coalesce(sum(cost_usd) FILTER (WHERE status = 'settled' AND created_at >= %(month_start)s), 0)
    AS month_usd,
  count(*) FILTER (WHERE status = 'in_flight' AND created_at > %(inflight_since)s) AS in_flight,
  count(*) FILTER (WHERE created_at > %(minute_start)s) AS minute_requests,
  min(created_at) FILTER (WHERE created_at > %(minute_start)s) AS minute_oldest,
  coalesce(sum(prompt_tokens + completion_tokens)
           FILTER (WHERE status = 'settled' AND settled_at > %(minute_start)s), 0) AS minute_tokens,
  min(settled_at) FILTER (WHERE status = 'settled' AND settled_at > %(minute_start)s)
    AS minute_oldest_settled
FROM llm_usage
WHERE agent_id = %(agent_id)s AND user_sub = %(user_sub)s
  AND (created_at >= least(%(month_start)s, %(minute_start)s, %(inflight_since)s)
       OR settled_at > %(minute_start)s)
"""

# 結算中的這一筆尚未 settled，因此只會算到其他已結算的請求
SETTLED_BEFORE = """
SELECT
  coalesce(sum(cost_usd) FILTER (WHERE created_at >= %(day_start)s AND created_at < %(day_end)s), 0)
    AS day_before,
  coalesce(sum(cost_usd) FILTER (
    WHERE created_at >= %(month_start)s AND created_at < %(month_end)s), 0) AS month_before,
  coalesce(sum(prompt_tokens + completion_tokens) FILTER (
    WHERE settled_at > %(minute_start)s AND settled_at <= %(at)s), 0) AS minute_tokens_before
FROM llm_usage
WHERE agent_id = %(agent_id)s AND user_sub = %(user_sub)s AND status = 'settled'
  AND ((created_at >= %(month_start)s AND created_at < %(month_end)s)
       OR settled_at > %(minute_start)s)
"""

SETTLE_UPDATE = """
UPDATE llm_usage SET
  status = 'settled', settled_at = %(at)s, settled_by = %(settled_by)s, cost_usd = %(cost)s,
  prompt_tokens = %(prompt_tokens)s, completion_tokens = %(completion_tokens)s,
  over_day_usd = %(over_day)s, over_month_usd = %(over_month)s, over_tpm_tokens = %(over_tpm)s
WHERE owner = %(owner)s AND call_id = %(call_id)s
"""

AUDIT_COLUMNS = (
    "id",
    "ts",
    "event_type",
    "agent_id",
    "agent_version",
    "key_alias",
    "user_sub",
    "username",
    "actor_sub",
    "actor_username",
    "server",
    "tool",
    "arguments",
    "decision",
    "reasons",
    "approval_id",
    "policy_version",
    "comment",
    "details",
    "prev_hash",
    "hash",
)
APPROVAL_FIELDS = (
    "id",
    "fingerprint",
    "status",
    "user_sub",
    "username",
    "server",
    "tool",
    "arguments",
    "reasons",
    "approver_role",
    "created_at",
    "expires_at",
    "decided_by",
    "decided_by_username",
    "decided_at",
    "comment",
    "used_at",
)
JSON_COLUMNS = {"arguments", "reasons", "details", "limits"}


def _db_value(column: str, value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if column in JSON_COLUMNS and value is not None:
        return Json(value)
    return value


def _approval_from_row(row: DictRow) -> Approval:
    agent = AgentRef(id=row["agent_id"], version=row["agent_version"], key_alias=row["key_alias"])
    return Approval(agent=agent, **{field: row[field] for field in APPROVAL_FIELDS})


def _insert(table: str, columns: dict[str, Any]) -> tuple[sql.Composed, list[Any]]:
    query = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
        sql.Identifier(table),
        sql.SQL(", ").join(sql.Identifier(c) for c in columns),
        sql.SQL(", ").join(sql.Placeholder() for _ in columns),
    )
    return query, [_db_value(c, v) for c, v in columns.items()]


async def _lock_user(conn: psycopg.AsyncConnection[DictRow], agent_id: str, user_sub: str) -> None:
    await conn.execute(
        "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
        (USAGE_LOCK_SPACE, f"{agent_id}:{user_sub}"),
    )


@asynccontextmanager
async def _ledger_errors() -> AsyncIterator[None]:
    # 帳本的呼叫端只認 LedgerUnavailable；DuplicateCallId 不是資料庫錯誤，照原樣往外傳
    try:
        yield
    except (StoreUnavailable, StoreError, psycopg.Error) as error:
        raise LedgerUnavailable(type(error).__name__) from error


async def _settle_context(
    conn: psycopg.AsyncConnection[DictRow], row: DictRow, settlement: Settlement, tz: ZoneInfo
) -> SettleContext:
    bounds = period_bounds(row["created_at"], tz)
    cursor = await conn.execute(
        SETTLED_BEFORE,
        {
            "agent_id": row["agent_id"],
            "user_sub": row["user_sub"],
            "day_start": bounds["day"][0],
            "day_end": bounds["day"][1],
            "month_start": bounds["month"][0],
            "month_end": bounds["month"][1],
            "minute_start": settlement.at - timedelta(seconds=60),
            "at": settlement.at,
        },
    )
    before = await cursor.fetchone()
    assert before is not None  # 聚合查詢一定回傳一列
    return SettleContext(
        limits=UsageLimits.model_validate(row["limits"]),
        cost=settlement.cost_usd,
        tokens=settlement.prompt_tokens + settlement.completion_tokens,
        day_before=before["day_before"],
        month_before=before["month_before"],
        minute_tokens_before=before["minute_tokens_before"],
    )


class PostgresStore:
    def __init__(self, database_url: str) -> None:
        self._url = database_url

    async def ensure_schema(self) -> None:
        await self._ensure_database()
        async with self._connect() as conn:
            await conn.execute(SCHEMA)

    async def _ensure_database(self) -> None:
        try:
            name = str(conninfo_to_dict(self._url).get("dbname") or "")
            admin_url = make_conninfo(self._url, dbname="postgres")
        except psycopg.Error:
            # 不串接原例外：訊息可能含連線字串
            raise StoreError("invalid database_url") from None
        if not name:
            raise StoreError("database_url has no dbname")
        try:
            async with await psycopg.AsyncConnection.connect(
                admin_url, autocommit=True, connect_timeout=CONNECT_TIMEOUT_SECONDS
            ) as conn:
                cursor = await conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
                if await cursor.fetchone() is None:
                    await conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        except psycopg.OperationalError as error:
            raise StoreUnavailable(str(error)) from error
        except psycopg.Error as error:
            raise StoreError(str(error)) from error

    @asynccontextmanager
    async def _connect(self) -> AsyncIterator[psycopg.AsyncConnection[DictRow]]:
        try:
            conn = await psycopg.AsyncConnection.connect(
                self._url, row_factory=dict_row, connect_timeout=CONNECT_TIMEOUT_SECONDS
            )
        except psycopg.OperationalError as error:
            raise StoreUnavailable(str(error)) from error
        try:
            async with conn:
                yield conn
        except psycopg.OperationalError as error:
            raise StoreUnavailable(str(error)) from error
        except psycopg.Error as error:
            raise StoreError(str(error)) from error

    async def open_approval(self, fingerprint: str) -> Approval | None:
        async with self._connect() as conn:
            cursor = await conn.execute(
                "SELECT * FROM approvals WHERE fingerprint = %s AND status = ANY(%s) "
                "ORDER BY created_at DESC LIMIT 1",
                (fingerprint, [ApprovalStatus.PENDING.value, ApprovalStatus.APPROVED.value]),
            )
            row = await cursor.fetchone()
        return _approval_from_row(row) if row else None

    async def approvals_for(self, fingerprint: str) -> list[Approval]:
        async with self._connect() as conn:
            cursor = await conn.execute(
                "SELECT * FROM approvals WHERE fingerprint = %s ORDER BY created_at", (fingerprint,)
            )
            rows = await cursor.fetchall()
        return [_approval_from_row(row) for row in rows]

    async def get_approval(self, approval_id: str) -> Approval | None:
        async with self._connect() as conn:
            cursor = await conn.execute("SELECT * FROM approvals WHERE id = %s", (approval_id,))
            row = await cursor.fetchone()
        return _approval_from_row(row) if row else None

    async def list_approvals(self, status: ApprovalStatus | None) -> list[Approval]:
        async with self._connect() as conn:
            if status is None:
                cursor = await conn.execute("SELECT * FROM approvals ORDER BY created_at DESC")
            else:
                cursor = await conn.execute(
                    "SELECT * FROM approvals WHERE status = %s ORDER BY created_at DESC",
                    (status.value,),
                )
            rows = await cursor.fetchall()
        return [_approval_from_row(row) for row in rows]

    async def create_approval(self, approval: Approval, audit: AuditEvent) -> AuditRecord:
        columns = approval.model_dump(exclude={"agent"}) | {
            "agent_id": approval.agent.id,
            "agent_version": approval.agent.version,
            "key_alias": approval.agent.key_alias,
        }
        async with self._connect() as conn:
            query, params = _insert("approvals", columns)
            try:
                await conn.execute(query, params)
            except psycopg.errors.UniqueViolation as error:
                if error.diag.constraint_name != ONE_OPEN_INDEX:
                    raise
                raise ApprovalConflict(approval.fingerprint) from error
            return await self._append(conn, audit, approval.created_at)

    async def transition(
        self,
        approval_id: str,
        *,
        from_statuses: tuple[ApprovalStatus, ...],
        to: ApprovalStatus,
        at: datetime,
        actor: UserIdentity | None = None,
        comment: str | None = None,
        audit: AuditEvent | None = None,
    ) -> bool:
        columns = transition_columns(to, at, actor, comment)
        query = sql.SQL("UPDATE approvals SET {} WHERE id = %s AND status = ANY(%s)").format(
            sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(c)) for c in columns)
        )
        params = [_db_value(c, v) for c, v in columns.items()]
        params += [approval_id, [s.value for s in from_statuses]]
        async with self._connect() as conn:
            cursor = await conn.execute(query, params)
            if cursor.rowcount != 1:
                return False
            if audit is not None:
                await self._append(conn, audit, at)
        return True

    async def append_audit(self, event: AuditEvent, ts: datetime) -> AuditRecord:
        async with self._connect() as conn:
            return await self._append(conn, event, ts)

    async def audit_records(self) -> list[AuditRecord]:
        async with self._connect() as conn:
            cursor = await conn.execute("SELECT * FROM audit_events ORDER BY id")
            rows = await cursor.fetchall()
        return [AuditRecord.model_validate(dict(row)) for row in rows]

    async def _append(
        self, conn: psycopg.AsyncConnection[DictRow], event: AuditEvent, ts: datetime
    ) -> AuditRecord:
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (AUDIT_LOCK_KEY,))
        cursor = await conn.execute("SELECT id, hash FROM audit_events ORDER BY id DESC LIMIT 1")
        last = await cursor.fetchone()
        record = build_record(
            record_id=(last["id"] + 1) if last else 1,
            prev_hash=last["hash"] if last else GENESIS_HASH,
            ts=ts,
            event=event,
        )
        query, params = _insert("audit_events", record.model_dump(include=set(AUDIT_COLUMNS)))
        await conn.execute(query, params)
        return record

    async def admit(
        self, admission: Admission, check: Callable[[AdmissionSnapshot], LimitDenial | None]
    ) -> LimitDenial | None:
        windows = admission.windows
        async with _ledger_errors(), self._connect() as conn:
            await _lock_user(conn, admission.agent_id, admission.user_sub)
            cursor = await conn.execute(
                ADMISSION_SNAPSHOT,
                {
                    "agent_id": admission.agent_id,
                    "user_sub": admission.user_sub,
                    "day_start": windows.day_start,
                    "month_start": windows.month_start,
                    "minute_start": windows.minute_start,
                    "inflight_since": admission.inflight_since,
                },
            )
            denial = check(AdmissionSnapshot.model_validate(await cursor.fetchone()))
            if denial is not None:
                return denial
            query, params = _insert(
                "llm_usage",
                {
                    "owner": admission.owner,
                    "call_id": admission.call_id,
                    "agent_id": admission.agent_id,
                    "user_sub": admission.user_sub,
                    "model": admission.model,
                    "tier": admission.limits.tier,
                    "limits": admission.limits.model_dump(mode="json"),
                    "status": "in_flight",
                    "created_at": admission.now,
                },
            )
            try:
                await conn.execute(query, params)
            except psycopg.errors.UniqueViolation as error:
                raise DuplicateCallId(admission.call_id) from error
        return None

    async def settle(
        self, settlement: Settlement, tz: ZoneInfo, build_audit: AuditBuilder
    ) -> Overage | None:
        try:
            return await self._settle(settlement, tz, build_audit)
        except Exception as error:
            # 建立或寫入稽核失敗時整個交易已回滾，帳本維持原狀
            raise LedgerUnavailable(type(error).__name__) from error

    async def _settle(
        self, settlement: Settlement, tz: ZoneInfo, build_audit: AuditBuilder
    ) -> Overage | None:
        key = (settlement.owner, settlement.call_id)
        async with self._connect() as conn:
            cursor = await conn.execute(
                "SELECT agent_id, user_sub, status FROM llm_usage "
                "WHERE owner = %s AND call_id = %s",
                key,
            )
            found = await cursor.fetchone()
            if found is None or found["status"] == "settled":
                return None
            await _lock_user(conn, found["agent_id"], found["user_sub"])
            cursor = await conn.execute(
                "SELECT * FROM llm_usage WHERE owner = %s AND call_id = %s FOR UPDATE", key
            )
            row = await cursor.fetchone()
            # 同一筆的另一個結算事件可能在等鎖期間完成
            if row is None or row["status"] == "settled":
                return None
            overage = compute_overage(await _settle_context(conn, row, settlement, tz))
            await conn.execute(
                SETTLE_UPDATE,
                {
                    "owner": settlement.owner,
                    "call_id": settlement.call_id,
                    "at": settlement.at,
                    "settled_by": settlement.settled_by,
                    "cost": settlement.cost_usd,
                    "prompt_tokens": settlement.prompt_tokens,
                    "completion_tokens": settlement.completion_tokens,
                    "over_day": overage.day,
                    "over_month": overage.month,
                    "over_tpm": overage.tpm_tokens,
                },
            )
            if overage.exceeded:
                # 已持有使用者鎖，_append 再取稽核鎖；順序固定，不會死鎖
                event = build_audit(SettledRow.model_validate(row), overage, settlement)
                await self._append(conn, event, settlement.at)
        return overage

    async def release(self, owner: str, call_id: str) -> bool:
        async with _ledger_errors(), self._connect() as conn:
            cursor = await conn.execute(
                "UPDATE llm_usage SET status = 'released' "
                "WHERE owner = %s AND call_id = %s AND status = 'in_flight'",
                (owner, call_id),
            )
            return cursor.rowcount == 1

    async def stale_in_flight(self, older_than: datetime, limit: int) -> list[tuple[str, str]]:
        async with _ledger_errors(), self._connect() as conn:
            cursor = await conn.execute(
                "SELECT owner, call_id FROM llm_usage WHERE status = 'in_flight' "
                "AND created_at < %s ORDER BY created_at LIMIT %s",
                (older_than, limit),
            )
            rows = await cursor.fetchall()
        return [(row["owner"], row["call_id"]) for row in rows]
