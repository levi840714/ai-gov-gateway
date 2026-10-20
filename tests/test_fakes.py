from datetime import datetime, timedelta

import pytest

from ai_gov_gateway.engine.audit import first_invalid
from ai_gov_gateway.engine.models import AgentRef, Approval, ApprovalStatus, AuditEvent
from ai_gov_gateway.engine.store import StoreUnavailable
from tests.fakes import FakeClock, InMemoryStore


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
