from datetime import timedelta

from ai_gov_gateway.engine.audit import first_invalid
from ai_gov_gateway.engine.models import AgentRef, Approval, ApprovalStatus, AuditEvent
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
