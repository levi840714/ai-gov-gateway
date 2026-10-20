from datetime import UTC, datetime, timedelta
from itertools import count
from typing import Any

from ai_gov_gateway.engine.audit import GENESIS_HASH, build_record
from ai_gov_gateway.engine.models import (
    Approval,
    ApprovalStatus,
    AuditEvent,
    AuditRecord,
    Decision,
    PolicyDecision,
    UserIdentity,
)
from ai_gov_gateway.engine.policy import PolicyError
from ai_gov_gateway.engine.store import transition_columns

OPEN = (ApprovalStatus.PENDING, ApprovalStatus.APPROVED)


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 20, 9, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


def id_sequence() -> Any:
    numbers = count(1)
    return lambda: f"apr_{next(numbers)}"


class InMemoryStore:
    def __init__(self) -> None:
        self.approvals: dict[str, Approval] = {}
        self.audit: list[AuditRecord] = []
        self.fail: Exception | None = None

    def _check(self) -> None:
        if self.fail is not None:
            raise self.fail

    def _append(self, event: AuditEvent, ts: datetime) -> AuditRecord:
        prev = self.audit[-1].hash if self.audit else GENESIS_HASH
        record = build_record(record_id=len(self.audit) + 1, prev_hash=prev, ts=ts, event=event)
        self.audit.append(record)
        return record

    async def open_approval(self, fingerprint: str) -> Approval | None:
        self._check()
        matches = [
            a for a in self.approvals.values() if a.fingerprint == fingerprint and a.status in OPEN
        ]
        return max(matches, key=lambda a: a.created_at, default=None)

    async def approvals_for(self, fingerprint: str) -> list[Approval]:
        self._check()
        matches = [a for a in self.approvals.values() if a.fingerprint == fingerprint]
        return sorted(matches, key=lambda a: a.created_at)

    async def get_approval(self, approval_id: str) -> Approval | None:
        self._check()
        return self.approvals.get(approval_id)

    async def list_approvals(self, status: ApprovalStatus | None) -> list[Approval]:
        self._check()
        rows = [a for a in self.approvals.values() if status is None or a.status is status]
        return sorted(rows, key=lambda a: a.created_at, reverse=True)

    async def create_approval(self, approval: Approval, audit: AuditEvent) -> AuditRecord:
        self._check()
        self.approvals[approval.id] = approval
        return self._append(audit, approval.created_at)

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
        self._check()
        approval = self.approvals.get(approval_id)
        if approval is None or approval.status not in from_statuses:
            return False
        update = transition_columns(to, at, actor, comment)
        self.approvals[approval_id] = approval.model_copy(update=update)
        if audit is not None:
            self._append(audit, at)
        return True

    async def append_audit(self, event: AuditEvent, ts: datetime) -> AuditRecord:
        self._check()
        return self._append(event, ts)

    async def audit_records(self) -> list[AuditRecord]:
        self._check()
        return list(self.audit)


def policy_result(decision: Decision, *reasons: str) -> PolicyDecision:
    approver = "compliance_supervisor" if decision is Decision.REQUIRE_APPROVAL else None
    return PolicyDecision(
        decision=decision, reasons=list(reasons), policy_version="test-1", approver_role=approver
    )


class FakePolicy:
    def __init__(self, decision: PolicyDecision | None = None) -> None:
        self.decision = decision or policy_result(Decision.ALLOW)
        self.error: str | None = None
        self.inputs: list[dict[str, Any]] = []

    async def evaluate(self, policy_input: dict[str, Any]) -> PolicyDecision:
        self.inputs.append(policy_input)
        if self.error is not None:
            raise PolicyError(self.error)
        return self.decision
