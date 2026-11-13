import asyncio
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import count
from typing import Any
from zoneinfo import ZoneInfo

from ai_gov_gateway.engine.audit import GENESIS_HASH, build_record
from ai_gov_gateway.engine.guardrails.models import GuardrailConfig, PiiSpan
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
    Period,
    SettleContext,
    compute_overage,
    period_bounds,
)
from ai_gov_gateway.engine.models import (
    AgentRef,
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


@dataclass
class UsageRow:
    admission: Admission
    status: str = "in_flight"
    cost: Decimal = Decimal("0")
    tokens: int = 0
    settled_at: datetime | None = None
    overage: Overage | None = None

    @property
    def created_at(self) -> datetime:
        return self.admission.now


class InMemoryLedger:
    """與 PostgresStore 的帳本相同規則；asyncio.Lock 取代（agent, 使用者）的 advisory lock。"""

    def __init__(self, store: InMemoryStore) -> None:
        self.store = store
        self.rows: dict[tuple[str, str], UsageRow] = {}
        self.fail: Exception | None = None
        self._lock = asyncio.Lock()

    def _check(self) -> None:
        if self.fail is not None:
            raise self.fail

    def _user_rows(self, admission: Admission) -> list[UsageRow]:
        user = (admission.agent_id, admission.user_sub)
        return [
            r for r in self.rows.values() if (r.admission.agent_id, r.admission.user_sub) == user
        ]

    def _snapshot(self, admission: Admission) -> AdmissionSnapshot:
        windows = admission.windows
        rows = self._user_rows(admission)
        settled = [r for r in rows if r.status == "settled"]
        minute = [r.created_at for r in rows if r.created_at > windows.minute_start]
        recent = [r for r in settled if r.settled_at and r.settled_at > windows.minute_start]
        return AdmissionSnapshot(
            day_usd=sum((r.cost for r in settled if r.created_at >= windows.day_start), Decimal(0)),
            month_usd=sum(
                (r.cost for r in settled if r.created_at >= windows.month_start), Decimal(0)
            ),
            in_flight=sum(
                1
                for r in rows
                if r.status == "in_flight" and r.created_at > admission.inflight_since
            ),
            minute_requests=len(minute),
            minute_oldest=min(minute, default=None),
            minute_tokens=sum(r.tokens for r in recent),
            minute_oldest_settled=min((r.settled_at for r in recent if r.settled_at), default=None),
        )

    async def admit(
        self, admission: Admission, check: Callable[[AdmissionSnapshot], LimitDenial | None]
    ) -> LimitDenial | None:
        async with self._lock:
            self._check()
            denial = check(self._snapshot(admission))
            if denial is not None:
                return denial
            key = (admission.owner, admission.call_id)
            if key in self.rows:
                raise DuplicateCallId(admission.call_id)
            self.rows[key] = UsageRow(admission=admission.model_copy(deep=True))
            return None

    def _context(self, row: UsageRow, settlement: Settlement, tz: ZoneInfo) -> SettleContext:
        bounds = period_bounds(row.created_at, tz)
        others = [r for r in self._user_rows(row.admission) if r.status == "settled"]
        minute_start = settlement.at - timedelta(seconds=60)

        def cost_in(period: Period) -> Decimal:
            start, end = bounds[period]
            return sum((r.cost for r in others if start <= r.created_at < end), Decimal(0))

        return SettleContext(
            limits=row.admission.limits,
            cost=settlement.cost_usd,
            tokens=settlement.prompt_tokens + settlement.completion_tokens,
            day_before=cost_in("day"),
            month_before=cost_in("month"),
            minute_tokens_before=sum(
                r.tokens
                for r in others
                if r.settled_at and minute_start < r.settled_at <= settlement.at
            ),
        )

    async def settle(
        self, settlement: Settlement, tz: ZoneInfo, build_audit: AuditBuilder
    ) -> Overage | None:
        async with self._lock:
            self._check()
            row = self.rows.get((settlement.owner, settlement.call_id))
            if row is None or row.status == "settled":
                return None
            overage = compute_overage(self._context(row, settlement, tz))
            if overage.exceeded:
                admission = row.admission
                settled = SettledRow(
                    agent_id=admission.agent_id,
                    key_alias=admission.key_alias,
                    user_sub=admission.user_sub,
                    model=admission.model,
                    tier=admission.limits.tier,
                    call_id=admission.call_id,
                    created_at=admission.now,
                )
                # 稽核失敗時帳本不變，與 Postgres 的交易回滾相同
                try:
                    await self.store.append_audit(
                        build_audit(settled, overage, settlement), settlement.at
                    )
                except Exception as error:
                    raise LedgerUnavailable(type(error).__name__) from error
            row.status = "settled"
            row.cost = settlement.cost_usd
            row.tokens = settlement.prompt_tokens + settlement.completion_tokens
            row.settled_at = settlement.at
            row.overage = overage
            return overage

    async def release(self, owner: str, call_id: str) -> bool:
        async with self._lock:
            self._check()
            row = self.rows.get((owner, call_id))
            if row is None or row.status != "in_flight":
                return False
            row.status = "released"
            return True

    async def stale_in_flight(self, older_than: datetime, limit: int) -> list[tuple[str, str]]:
        self._check()
        rows = [
            r for r in self.rows.values() if r.status == "in_flight" and r.created_at < older_than
        ]
        rows.sort(key=lambda r: r.created_at)
        return [(r.admission.owner, r.admission.call_id) for r in rows[:limit]]


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
    "limits": {
        "tier": "default",
        "budget_usd": {"day": "2.0", "month": "20.0"},
        "rpm": 20,
        "tpm": 150000,
        "max_concurrent": 4,
        "max_input_chars": 100000,
        "max_output_tokens": 4096,
    },
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
        self.agents: list[AgentRef] = []
        self.user: UserIdentity | None = None

    async def llm_config(
        self, agent: AgentRef, user: UserIdentity | None = None
    ) -> GuardrailConfig:
        self.agents.append(agent)
        self.user = user
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
