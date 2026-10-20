from datetime import timedelta

import pytest

from ai_gov_gateway.engine.approvals import ApprovalError, ApprovalService
from ai_gov_gateway.engine.engine import GovernanceEngine
from ai_gov_gateway.engine.models import (
    ApprovalStatus,
    Decision,
    PolicyDecision,
    ToolCall,
    UserIdentity,
)
from tests.fakes import FakeClock, FakePolicy, InMemoryStore, id_sequence, policy_result
from tests.jwt_helpers import alice_token, make_verifier

ALICE = UserIdentity(sub="alice-sub", username="alice", roles=["analyst", "compliance_supervisor"])
BOB = UserIdentity(sub="bob-sub", username="bob", roles=["compliance_supervisor"])
ANALYST = UserIdentity(sub="dave-sub", username="dave", roles=["analyst"])


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture
def service(store, clock) -> ApprovalService:
    return ApprovalService(store=store, approver_role="compliance_supervisor", clock=clock)


@pytest.fixture
def engine(store, clock) -> GovernanceEngine:
    return GovernanceEngine(
        verifier=make_verifier(),
        policy=FakePolicy(policy_result(Decision.REQUIRE_APPROVAL, "amount_over_threshold")),
        store=store,
        approval_ttl=timedelta(hours=24),
        approver_role="compliance_supervisor",
        clock=clock,
        new_id=id_sequence(),
    )


@pytest.fixture
def pending(engine):
    async def create() -> str:
        call = ToolCall(
            key_alias="kyt-agent@1.4",
            user_token=alice_token(),
            server="account_ops",
            tool="freeze_account",
            arguments={"account_id": "acct-001", "amount_usd": 25000, "counterparty": "0x1"},
        )
        outcome = await engine.decide(call)
        assert outcome.approval_id is not None
        return outcome.approval_id

    return create


async def test_other_supervisor_approves(service, store, pending):
    approval_id = await pending()
    approved = await service.approve(BOB, approval_id, "已確認")
    assert approved.status is ApprovalStatus.APPROVED
    assert (approved.decided_by_username, approved.comment) == ("bob", "已確認")
    event = store.audit[-1]
    assert (event.event_type, event.actor_username, event.comment) == (
        "approval_approved",
        "bob",
        "已確認",
    )


async def test_delegator_cannot_approve_own_request(service, pending):
    approval_id = await pending()
    with pytest.raises(ApprovalError) as error:
        await service.approve(ALICE, approval_id, "")
    assert (error.value.status_code, error.value.code) == (403, "self_approval_forbidden")


async def test_delegator_may_reject_own_request(service, pending):
    approval_id = await pending()
    rejected = await service.reject(ALICE, approval_id, "撤回")
    assert rejected.status is ApprovalStatus.REJECTED


async def test_role_required(service, pending):
    approval_id = await pending()
    for action in (service.list(ANALYST, None), service.approve(ANALYST, approval_id, "")):
        with pytest.raises(ApprovalError) as error:
            await action
        assert (error.value.status_code, error.value.code) == (403, "role_required")


async def test_decided_approval_cannot_be_decided_again(service, pending):
    approval_id = await pending()
    await service.reject(BOB, approval_id, "")
    with pytest.raises(ApprovalError) as error:
        await service.approve(BOB, approval_id, "")
    assert (error.value.status_code, error.value.code) == (409, "approval_not_pending")


async def test_expired_approval_cannot_be_approved(service, store, clock, pending):
    approval_id = await pending()
    clock.advance(hours=25)
    with pytest.raises(ApprovalError) as error:
        await service.approve(BOB, approval_id, "")
    assert (error.value.status_code, error.value.code) == (409, "approval_expired")
    assert store.approvals[approval_id].status is ApprovalStatus.EXPIRED
    assert store.audit[-1].event_type == "approval_expired"


async def test_unknown_approval(service):
    with pytest.raises(ApprovalError) as error:
        await service.get(BOB, "apr_missing")
    assert (error.value.status_code, error.value.code) == (404, "approval_not_found")


async def test_detail_includes_history_and_list_filters(service, clock, pending):
    first = await pending()
    await service.reject(BOB, first, "金額有誤")
    clock.advance(minutes=1)
    second = await pending()
    detail = await service.get(BOB, second)
    assert [a.id for a in detail.history] == [first]
    assert [a.id for a in await service.list(BOB, ApprovalStatus.PENDING)] == [second]


async def test_verify_audit(service, store, pending):
    await pending()
    result = await service.verify_audit(BOB)
    assert (result.records, result.first_invalid_id) == (1, None)
    store.audit[0] = store.audit[0].model_copy(update={"username": "mallory"})
    assert (await service.verify_audit(BOB)).first_invalid_id == 1


async def test_list_expired_includes_due_approvals_not_yet_marked(service, clock, pending):
    approval_id = await pending()
    clock.advance(hours=25)
    expired = await service.list(BOB, ApprovalStatus.EXPIRED)
    assert [(a.id, a.status) for a in expired] == [(approval_id, ApprovalStatus.EXPIRED)]


async def test_approval_requires_its_own_approver_role(service, store, clock):
    engine = GovernanceEngine(
        verifier=make_verifier(),
        policy=FakePolicy(
            PolicyDecision(
                decision=Decision.REQUIRE_APPROVAL,
                reasons=["amount_over_threshold"],
                policy_version="test-1",
                approver_role="other_role",
            )
        ),
        store=store,
        approval_ttl=timedelta(hours=24),
        approver_role="compliance_supervisor",
        clock=clock,
        new_id=id_sequence(),
    )
    call = ToolCall(
        key_alias="kyt-agent@1.4",
        user_token=alice_token(),
        server="account_ops",
        tool="freeze_account",
        arguments={"account_id": "acct-001"},
    )
    outcome = await engine.decide(call)
    assert outcome.approval_id is not None
    with pytest.raises(ApprovalError) as error:
        await service.approve(BOB, outcome.approval_id, "")
    assert (error.value.status_code, error.value.code) == (403, "role_required")
