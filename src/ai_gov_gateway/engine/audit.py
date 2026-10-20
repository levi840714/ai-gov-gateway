from collections.abc import Sequence
from datetime import UTC, datetime

from ai_gov_gateway.engine.fingerprint import canonical_json, sha256_hex
from ai_gov_gateway.engine.models import (
    Approval,
    AuditEvent,
    AuditEventType,
    AuditRecord,
    UserIdentity,
)

GENESIS_HASH = "0" * 64


def record_hash(prev_hash: str, ts: datetime, event: AuditEvent) -> str:
    if ts.tzinfo is None:
        raise ValueError("audit timestamps must be timezone-aware")
    # 資料庫讀回的時間可能帶不同時區，統一換成 UTC 再算；
    # 排除 None，之後新增的選填欄位才不會改變既有紀錄的 hash
    body = {
        "prev_hash": prev_hash,
        "ts": ts.astimezone(UTC).isoformat(),
        "event": event.model_dump(mode="json", exclude_none=True),
    }
    return sha256_hex(canonical_json(body))


def build_record(*, record_id: int, prev_hash: str, ts: datetime, event: AuditEvent) -> AuditRecord:
    return AuditRecord(
        id=record_id,
        ts=ts,
        prev_hash=prev_hash,
        hash=record_hash(prev_hash, ts, event),
        **event.model_dump(),
    )


def event_of(record: AuditRecord) -> AuditEvent:
    return AuditEvent.model_validate(record.model_dump(include=set(AuditEvent.model_fields)))


def first_invalid(records: Sequence[AuditRecord]) -> int | None:
    """回傳第一筆 hash 對不上的 id；整條 chain 正確時回傳 None。"""
    prev = GENESIS_HASH
    for record in records:
        if (
            record.prev_hash != prev
            or record_hash(prev, record.ts, event_of(record)) != record.hash
        ):
            return record.id
        prev = record.hash
    return None


def approval_event(
    event_type: AuditEventType,
    approval: Approval,
    *,
    actor: UserIdentity | None = None,
    decision: str | None = None,
    comment: str | None = None,
) -> AuditEvent:
    return AuditEvent(
        event_type=event_type,
        agent_id=approval.agent.id,
        agent_version=approval.agent.version,
        key_alias=approval.agent.key_alias,
        user_sub=approval.user_sub,
        username=approval.username,
        actor_sub=actor.sub if actor else None,
        actor_username=actor.username if actor else None,
        server=approval.server,
        tool=approval.tool,
        arguments=approval.arguments,
        decision=decision,
        reasons=approval.reasons,
        approval_id=approval.id,
        comment=comment,
    )
