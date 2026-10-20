from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from enum import StrEnum
from typing import Any

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Json

from ai_gov_gateway.engine.audit import GENESIS_HASH, build_record
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
ONE_OPEN_INDEX = "approvals_one_open"

# arguments 與 reasons 用 json 而非 jsonb：jsonb 會改寫數字（1e22、-0.0），
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
    prev_hash text NOT NULL,
    hash text NOT NULL
);
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
JSON_COLUMNS = {"arguments", "reasons"}


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


class PostgresStore:
    def __init__(self, database_url: str) -> None:
        self._url = database_url

    async def ensure_schema(self) -> None:
        await self._ensure_database()
        async with self._connect() as conn:
            await conn.execute(SCHEMA)

    async def _ensure_database(self) -> None:
        name = str(conninfo_to_dict(self._url).get("dbname") or "")
        if not name:
            raise StoreError("database_url has no dbname")
        admin_url = make_conninfo(self._url, dbname="postgres")
        try:
            async with await psycopg.AsyncConnection.connect(admin_url, autocommit=True) as conn:
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
            conn = await psycopg.AsyncConnection.connect(self._url, row_factory=dict_row)
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
