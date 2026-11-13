from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from ai_gov_gateway.engine.audit import first_invalid
from ai_gov_gateway.engine.ledger import (
    Admission,
    DuplicateCallId,
    LedgerUnavailable,
    SettledRow,
    Settlement,
)
from ai_gov_gateway.engine.limits import Overage, UsageLimits, usage_windows
from ai_gov_gateway.engine.models import AgentRef, Approval, ApprovalStatus, AuditEvent
from ai_gov_gateway.engine.store import ApprovalConflict, StoreUnavailable
from tests.fakes import GUARDRAIL_CONFIG, FakeClock, InMemoryLedger, InMemoryStore

LEDGER_TZ = ZoneInfo("Asia/Taipei")


def _approval(clock: FakeClock) -> Approval:
    return Approval(
        id="apr_1",
        fingerprint="f",
        status=ApprovalStatus.PENDING,
        agent=AgentRef(id="kyt-agent", version="1.4", key_alias="kyt-agent@1.4"),
        user_sub="alice-sub",
        username="alice",
        server="account_ops",
        tool="freeze_account",
        arguments={},
        reasons=[],
        approver_role="compliance_supervisor",
        created_at=clock(),
        expires_at=clock() + timedelta(hours=24),
    )


async def test_transition_only_from_allowed_statuses():
    clock, store = FakeClock(), InMemoryStore()
    await store.create_approval(_approval(clock), AuditEvent(event_type="approval_requested"))
    used = await store.transition(
        "apr_1", from_statuses=(ApprovalStatus.APPROVED,), to=ApprovalStatus.USED, at=clock()
    )
    assert used is False
    approved = await store.transition(
        "apr_1",
        from_statuses=(ApprovalStatus.PENDING,),
        to=ApprovalStatus.APPROVED,
        at=clock(),
        audit=AuditEvent(event_type="approval_approved"),
    )
    assert approved is True
    assert (await store.get_approval("apr_1")).status is ApprovalStatus.APPROVED
    assert [r.event_type for r in store.audit] == ["approval_requested", "approval_approved"]
    assert first_invalid(store.audit) is None


async def test_refused_transition_with_audit_writes_nothing():
    clock, store = FakeClock(), InMemoryStore()
    await store.create_approval(_approval(clock), AuditEvent(event_type="approval_requested"))
    refused = await store.transition(
        "apr_1",
        from_statuses=(ApprovalStatus.APPROVED,),
        to=ApprovalStatus.USED,
        at=clock(),
        audit=AuditEvent(event_type="approval_redeemed"),
    )
    assert refused is False
    assert [r.event_type for r in store.audit] == ["approval_requested"]


async def test_transition_of_unknown_id_returns_false():
    clock, store = FakeClock(), InMemoryStore()
    moved = await store.transition(
        "missing", from_statuses=(ApprovalStatus.PENDING,), to=ApprovalStatus.APPROVED, at=clock()
    )
    assert moved is False


@pytest.mark.parametrize(
    "status", [ApprovalStatus.REJECTED, ApprovalStatus.USED, ApprovalStatus.EXPIRED]
)
async def test_open_approval_ignores_terminal_statuses(status):
    clock, store = FakeClock(), InMemoryStore()
    await store.create_approval(
        _approval(clock).model_copy(update={"status": status}),
        AuditEvent(event_type="approval_requested"),
    )
    assert await store.open_approval("f") is None


async def test_fail_makes_calls_raise():
    store = InMemoryStore()
    store.fail = StoreUnavailable("x")
    with pytest.raises(StoreUnavailable):
        await store.get_approval("apr_1")
    with pytest.raises(StoreUnavailable):
        await store.audit_records()


async def test_failed_audit_leaves_approval_unchanged():
    clock, store = FakeClock(), InMemoryStore()
    await store.create_approval(_approval(clock), AuditEvent(event_type="approval_requested"))
    with pytest.raises(ValueError):
        await store.transition(
            "apr_1",
            from_statuses=(ApprovalStatus.PENDING,),
            to=ApprovalStatus.APPROVED,
            at=datetime(2026, 10, 20, 9),
            audit=AuditEvent(event_type="approval_approved"),
        )
    assert (await store.get_approval("apr_1")).status is ApprovalStatus.PENDING
    assert len(store.audit) == 1


async def test_reads_return_copies():
    clock, store = FakeClock(), InMemoryStore()
    await store.create_approval(_approval(clock), AuditEvent(event_type="approval_requested"))
    (await store.get_approval("apr_1")).status = ApprovalStatus.USED
    (await store.open_approval("f")).arguments["x"] = 1
    assert store.approvals["apr_1"].status is ApprovalStatus.PENDING
    assert store.approvals["apr_1"].arguments == {}


async def test_create_approval_rejects_second_open_approval_for_fingerprint():
    clock, store = FakeClock(), InMemoryStore()
    await store.create_approval(_approval(clock), AuditEvent(event_type="approval_requested"))
    second = _approval(clock).model_copy(update={"id": "apr_2"})
    with pytest.raises(ApprovalConflict):
        await store.create_approval(second, AuditEvent(event_type="approval_requested"))
    assert list(store.approvals) == ["apr_1"]
    assert len(store.audit) == 1


def _ledger_admission(call_id: str, now: datetime) -> Admission:
    return Admission(
        owner="owner-a",
        call_id=call_id,
        agent_id="kyt-agent",
        key_alias="kyt-agent@1.6",
        user_sub="alice-sub",
        model="gemini",
        limits=UsageLimits.model_validate(GUARDRAIL_CONFIG["limits"]),
        windows=usage_windows(now, LEDGER_TZ),
        inflight_since=now - timedelta(seconds=300),
        now=now,
    )


def _ledger_settlement(call_id: str, cost: str, at: datetime) -> Settlement:
    return Settlement(
        owner="owner-a",
        call_id=call_id,
        cost_usd=Decimal(cost),
        prompt_tokens=100,
        completion_tokens=50,
        at=at,
        settled_by="callback",
    )


def _exceeded(row: SettledRow, overage: Overage, settlement: Settlement) -> AuditEvent:
    return AuditEvent(
        event_type="llm_limit_exceeded", key_alias=row.key_alias, reasons=overage.exceeded
    )


async def test_ledger_settles_overage_into_store_audit():
    clock, store = FakeClock(), InMemoryStore()
    ledger = InMemoryLedger(store)
    for call_id, cost in (("c1", "1.99"), ("c2", "0.03")):
        assert await ledger.admit(_ledger_admission(call_id, clock()), lambda _: None) is None
        overage = await ledger.settle(
            _ledger_settlement(call_id, cost, clock()), LEDGER_TZ, _exceeded
        )
    assert overage is not None and overage.day == Decimal("0.02")
    assert [(r.key_alias, r.reasons) for r in store.audit] == [("kyt-agent@1.6", ["budget_day"])]
    assert await ledger.settle(_ledger_settlement("c2", "1", clock()), LEDGER_TZ, _exceeded) is None


async def test_ledger_audit_failure_leaves_row_in_flight():
    clock, store = FakeClock(), InMemoryStore()
    ledger = InMemoryLedger(store)
    await ledger.admit(_ledger_admission("c1", clock()), lambda _: None)
    store.fail = StoreUnavailable("down")
    with pytest.raises(LedgerUnavailable):
        await ledger.settle(_ledger_settlement("c1", "3", clock()), LEDGER_TZ, _exceeded)
    assert ledger.rows[("owner-a", "c1")].status == "in_flight"
    with pytest.raises(DuplicateCallId):
        await ledger.admit(_ledger_admission("c1", clock()), lambda _: None)
