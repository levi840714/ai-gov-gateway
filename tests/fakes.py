import re
import threading
from datetime import UTC, datetime, timedelta
from itertools import count
from typing import Any

from ai_gov_gateway.engine.audit import GENESIS_HASH, build_record
from ai_gov_gateway.engine.guardrails.models import GuardrailConfig, PiiSpan
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
from ai_gov_gateway.engine.store import ApprovalConflict, transition_columns

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

    def _next_record(self, event: AuditEvent, ts: datetime) -> AuditRecord:
        prev = self.audit[-1].hash if self.audit else GENESIS_HASH
        return build_record(record_id=len(self.audit) + 1, prev_hash=prev, ts=ts, event=event)

    async def open_approval(self, fingerprint: str) -> Approval | None:
        self._check()
        matches = [
            a for a in self.approvals.values() if a.fingerprint == fingerprint and a.status in OPEN
        ]
        latest = max(matches, key=lambda a: a.created_at, default=None)
        return latest.model_copy(deep=True) if latest else None

    async def approvals_for(self, fingerprint: str) -> list[Approval]:
        self._check()
        matches = [a for a in self.approvals.values() if a.fingerprint == fingerprint]
        return [a.model_copy(deep=True) for a in sorted(matches, key=lambda a: a.created_at)]

    async def get_approval(self, approval_id: str) -> Approval | None:
        self._check()
        approval = self.approvals.get(approval_id)
        return approval.model_copy(deep=True) if approval else None

    async def list_approvals(self, status: ApprovalStatus | None) -> list[Approval]:
        self._check()
        rows = [a for a in self.approvals.values() if status is None or a.status is status]
        rows.sort(key=lambda a: a.created_at, reverse=True)
        return [a.model_copy(deep=True) for a in rows]

    async def create_approval(self, approval: Approval, audit: AuditEvent) -> AuditRecord:
        self._check()
        if any(
            a.fingerprint == approval.fingerprint and a.status in OPEN
            for a in self.approvals.values()
        ):
            raise ApprovalConflict(approval.fingerprint)
        record = self._next_record(audit, approval.created_at)
        self.approvals[approval.id] = approval.model_copy(deep=True)
        self.audit.append(record)
        return record.model_copy(deep=True)

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
        record = self._next_record(audit, at) if audit is not None else None
        self.approvals[approval_id] = approval.model_copy(update=update)
        if record is not None:
            self.audit.append(record)
        return True

    async def append_audit(self, event: AuditEvent, ts: datetime) -> AuditRecord:
        self._check()
        record = self._next_record(event, ts)
        self.audit.append(record)
        return record.model_copy(deep=True)

    async def audit_records(self) -> list[AuditRecord]:
        self._check()
        return [r.model_copy(deep=True) for r in self.audit]


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


GUARDRAIL_CONFIG: dict[str, Any] = {
    "agent_status": "active",
    "pii": {"entities": ["EMAIL"], "exclude": ["eth_address"]},
    "injection": {"action": "redact", "system_notice": False},
    "high_risk_tools": ["file_str"],
    "policy_version": "test-1",
}


def guardrail_config(**updates: Any) -> GuardrailConfig:
    data = dict(GUARDRAIL_CONFIG)
    for key, value in updates.items():
        if key in ("action", "system_notice"):
            data["injection"] = {**data["injection"], key: value}
        else:
            data[key] = value
    return GuardrailConfig.model_validate(data)


class FakeGuardrailPolicy:
    def __init__(self, config: GuardrailConfig | None = None) -> None:
        self.config = config or guardrail_config()
        self.error: str | None = None
        self.agents: list[str] = []

    async def llm_config(self, agent_id: str) -> GuardrailConfig:
        self.agents.append(agent_id)
        if self.error is not None:
            raise PolicyError(self.error)
        return self.config


class FakePiiDetector:
    """只認得字面上的 email，讓 service 的測試不依賴 Presidio。"""

    def __init__(self) -> None:
        self.fail: Exception | None = None
        self.pattern = re.compile(r"[a-z]+@example\.com")
        self.threads: set[str] = set()

    def detect(self, text: str, entities: list[str]) -> list[PiiSpan]:
        self.threads.add(threading.current_thread().name)
        if self.fail is not None:
            raise self.fail
        if "EMAIL" not in entities:
            return []
        return [
            PiiSpan(entity="EMAIL", start=m.start(), end=m.end())
            for m in self.pattern.finditer(text)
        ]
