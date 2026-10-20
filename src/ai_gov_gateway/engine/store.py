from datetime import datetime
from typing import Any, Protocol

from ai_gov_gateway.engine.models import (
    Approval,
    ApprovalStatus,
    AuditEvent,
    AuditRecord,
    UserIdentity,
)


class StoreUnavailable(Exception):
    """資料庫連不上。"""


class StoreError(Exception):
    """資料庫操作失敗（連線以外的原因）。"""


class ApprovalConflict(Exception):
    """同一 fingerprint 已有 pending 或 approved 的審批。"""


class GovernanceStore(Protocol):
    async def open_approval(self, fingerprint: str) -> Approval | None:
        """同一 fingerprint 中最新一筆 pending 或 approved 的審批。"""
        ...

    async def approvals_for(self, fingerprint: str) -> list[Approval]: ...

    async def get_approval(self, approval_id: str) -> Approval | None: ...

    async def list_approvals(self, status: ApprovalStatus | None) -> list[Approval]: ...

    async def create_approval(self, approval: Approval, audit: AuditEvent) -> AuditRecord:
        """建立審批並在同一個 transaction 寫入稽核。

        同一 fingerprint 已有 pending 或 approved 的審批時 raise ApprovalConflict，
        實作必須保證每個 fingerprint 至多一筆未結案審批。
        """
        ...

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
        """狀態在 from_statuses 中才轉換；轉換與稽核同一個 transaction，回傳是否成功。"""
        ...

    async def append_audit(self, event: AuditEvent, ts: datetime) -> AuditRecord: ...

    async def audit_records(self) -> list[AuditRecord]: ...


def transition_columns(
    to: ApprovalStatus, at: datetime, actor: UserIdentity | None, comment: str | None
) -> dict[str, Any]:
    columns: dict[str, Any] = {"status": to}
    if to in (ApprovalStatus.APPROVED, ApprovalStatus.REJECTED):
        columns |= {
            "decided_by": actor.sub if actor else None,
            "decided_by_username": actor.username if actor else None,
            "decided_at": at,
            "comment": comment,
        }
    if to is ApprovalStatus.USED:
        columns["used_at"] = at
    return columns
