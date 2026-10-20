from pydantic import BaseModel

from ai_gov_gateway.engine.audit import approval_event, first_invalid
from ai_gov_gateway.engine.clock import Clock, utc_now
from ai_gov_gateway.engine.models import (
    Approval,
    ApprovalStatus,
    AuditEventType,
    UserIdentity,
)
from ai_gov_gateway.engine.store import GovernanceStore

OPEN_STATUSES = (ApprovalStatus.PENDING, ApprovalStatus.APPROVED)


class ApprovalError(Exception):
    def __init__(self, status_code: int, code: str) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code


class ApprovalDetail(BaseModel):
    approval: Approval
    history: list[Approval]


class AuditVerification(BaseModel):
    records: int
    first_invalid_id: int | None


class ApprovalService:
    def __init__(
        self, *, store: GovernanceStore, approver_role: str, clock: Clock = utc_now
    ) -> None:
        self._store = store
        self._approver_role = approver_role
        self._clock = clock

    async def list(self, actor: UserIdentity, status: ApprovalStatus | None) -> list[Approval]:
        self._require_role(actor)
        refreshed = [await self._expire_if_due(a) for a in await self._store.list_approvals(status)]
        return [a for a in refreshed if status is None or a.status is status]

    async def get(self, actor: UserIdentity, approval_id: str) -> ApprovalDetail:
        self._require_role(actor)
        approval = await self._load(approval_id)
        history = [
            a for a in await self._store.approvals_for(approval.fingerprint) if a.id != approval.id
        ]
        return ApprovalDetail(approval=approval, history=history)

    async def approve(self, actor: UserIdentity, approval_id: str, comment: str) -> Approval:
        return await self._decide(actor, approval_id, comment, ApprovalStatus.APPROVED)

    async def reject(self, actor: UserIdentity, approval_id: str, comment: str) -> Approval:
        return await self._decide(actor, approval_id, comment, ApprovalStatus.REJECTED)

    async def verify_audit(self, actor: UserIdentity) -> AuditVerification:
        self._require_role(actor)
        records = await self._store.audit_records()
        return AuditVerification(records=len(records), first_invalid_id=first_invalid(records))

    async def _decide(
        self, actor: UserIdentity, approval_id: str, comment: str, to: ApprovalStatus
    ) -> Approval:
        self._require_role(actor)
        approval = await self._load(approval_id)
        if approval.status is ApprovalStatus.EXPIRED:
            raise ApprovalError(409, "approval_expired")
        if approval.status is not ApprovalStatus.PENDING:
            raise ApprovalError(409, "approval_not_pending")
        if to is ApprovalStatus.APPROVED and actor.sub == approval.user_sub:
            raise ApprovalError(403, "self_approval_forbidden")
        event_type: AuditEventType = (
            "approval_approved" if to is ApprovalStatus.APPROVED else "approval_rejected"
        )
        changed = await self._store.transition(
            approval_id,
            from_statuses=(ApprovalStatus.PENDING,),
            to=to,
            at=self._clock(),
            actor=actor,
            comment=comment,
            audit=approval_event(event_type, approval, actor=actor, decision=to, comment=comment),
        )
        if not changed:
            raise ApprovalError(409, "approval_not_pending")
        return await self._load(approval_id)

    async def _load(self, approval_id: str) -> Approval:
        approval = await self._store.get_approval(approval_id)
        if approval is None:
            raise ApprovalError(404, "approval_not_found")
        return await self._expire_if_due(approval)

    async def _expire_if_due(self, approval: Approval) -> Approval:
        now = self._clock()
        if approval.status not in OPEN_STATUSES or approval.expires_at > now:
            return approval
        await self._store.transition(
            approval.id,
            from_statuses=OPEN_STATUSES,
            to=ApprovalStatus.EXPIRED,
            at=now,
            audit=approval_event("approval_expired", approval, decision=ApprovalStatus.EXPIRED),
        )
        refreshed = await self._store.get_approval(approval.id)
        return refreshed or approval

    def _require_role(self, actor: UserIdentity) -> None:
        if self._approver_role not in actor.roles:
            raise ApprovalError(403, "role_required")
