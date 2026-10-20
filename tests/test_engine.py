import asyncio
from datetime import timedelta

import pytest

from ai_gov_gateway.engine.audit import first_invalid
from ai_gov_gateway.engine.engine import GovernanceEngine
from ai_gov_gateway.engine.models import ApprovalStatus, Decision, ToolCall
from ai_gov_gateway.engine.store import ApprovalConflict, StoreError, StoreUnavailable
from tests.fakes import FakeClock, FakePolicy, InMemoryStore, id_sequence, policy_result
from tests.jwt_helpers import alice_token, make_token, make_verifier


class InterleavingStore(InMemoryStore):
    """open_approval 讓出控制權，讓並發請求交錯。"""

    async def open_approval(self, fingerprint):
        result = await super().open_approval(fingerprint)
        await asyncio.sleep(0)
        return result


class AlwaysConflictStore(InMemoryStore):
    async def open_approval(self, fingerprint):
        return None

    async def create_approval(self, approval, audit):
        raise ApprovalConflict(approval.fingerprint)


NEEDS_APPROVAL = policy_result(Decision.REQUIRE_APPROVAL, "amount_over_threshold")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture
def policy() -> FakePolicy:
    return FakePolicy()


@pytest.fixture
def engine(clock, store, policy) -> GovernanceEngine:
    return GovernanceEngine(
        verifier=make_verifier(),
        policy=policy,
        store=store,
        approval_ttl=timedelta(hours=24),
        approver_role="compliance_supervisor",
        clock=clock,
        new_id=id_sequence(),
    )


def freeze(amount: int = 25000, *, key: str | None = "kyt-agent@1.4", token=None) -> ToolCall:
    return ToolCall(
        key_alias=key,
        user_token=alice_token() if token is None else token,
        server="account_ops",
        tool="freeze_account",
        arguments={"account_id": "acct-001", "amount_usd": amount, "counterparty": "0x1"},
    )


async def test_allow_is_audited_with_policy_input(engine, store, policy):
    outcome = await engine.decide(freeze(5000))
    assert outcome.status == "allowed"
    assert policy.inputs[0]["agent"] == {
        "id": "kyt-agent",
        "version": "1.4",
        "key_alias": "kyt-agent@1.4",
    }
    assert policy.inputs[0]["user"]["roles"] == ["analyst"]
    assert policy.inputs[0]["tool"] == {"server": "account_ops", "name": "freeze_account"}
    [record] = store.audit
    assert (record.event_type, record.decision, record.policy_version) == (
        "decision",
        "allowed",
        "test-1",
    )
    assert record.username == "alice"


async def test_policy_deny_is_returned_and_audited(engine, store, policy):
    policy.decision = policy_result(Decision.DENY, "role_required")
    outcome = await engine.decide(freeze())
    assert (outcome.status, outcome.reasons) == ("denied", ["role_required"])
    assert store.audit[0].reasons == ["role_required"]


@pytest.mark.parametrize(
    ("call_kwargs", "reason"),
    [
        ({"token": ""}, "missing_user_token"),
        ({"token": make_token("alice", ["analyst"], expires_in=-10)}, "invalid_user_token"),
        ({"key": "kyt-agent-mcp"}, "unregistered_agent_version"),
        ({"key": None}, "unregistered_agent_version"),
        ({"key": "kyt-agent-mcp", "token": ""}, "missing_user_token"),
    ],
)
async def test_rejected_before_policy(engine, store, policy, call_kwargs, reason):
    outcome = await engine.decide(freeze(**call_kwargs))
    assert (outcome.status, outcome.reasons) == ("denied", [reason])
    assert policy.inputs == []
    assert store.audit[0].reasons == [reason]


@pytest.mark.parametrize("key", ["kyt-agent-mcp", None])
async def test_unregistered_agent_is_attributed_to_delegating_user(engine, store, key):
    await engine.decide(freeze(key=key))
    assert (store.audit[0].username, store.audit[0].user_sub) == ("alice", "alice-sub")


async def test_policy_unavailable_denies(engine, store, policy):
    policy.error = "policy_unavailable"
    outcome = await engine.decide(freeze())
    assert outcome.reasons == ["policy_unavailable"]
    assert store.audit[0].decision == "denied"


async def test_require_approval_creates_one_pending_approval(engine, store, policy):
    policy.decision = NEEDS_APPROVAL
    first = await engine.decide(freeze())
    second = await engine.decide(freeze())
    assert first.status == "pending_approval"
    assert first.approval_id == second.approval_id == "apr_1"
    assert first.expires_at is not None
    assert list(store.approvals) == ["apr_1"]
    assert [r.event_type for r in store.audit] == ["approval_requested", "decision"]


async def test_approved_call_is_redeemed_once(engine, store, policy, clock):
    policy.decision = NEEDS_APPROVAL
    await engine.decide(freeze())
    await store.transition(
        "apr_1", from_statuses=(ApprovalStatus.PENDING,), to=ApprovalStatus.APPROVED, at=clock()
    )
    redeemed = await engine.decide(freeze())
    again = await engine.decide(freeze())
    assert redeemed.status == "allowed"
    assert store.approvals["apr_1"].status is ApprovalStatus.USED
    assert (again.status, again.approval_id) == ("pending_approval", "apr_2")
    assert "approval_redeemed" in [r.event_type for r in store.audit]
    assert first_invalid(store.audit) is None


async def test_new_policy_reason_needs_new_approval(engine, store, policy, clock):
    policy.decision = NEEDS_APPROVAL
    await engine.decide(freeze())
    await store.transition(
        "apr_1", from_statuses=(ApprovalStatus.PENDING,), to=ApprovalStatus.APPROVED, at=clock()
    )
    policy.decision = policy_result(
        Decision.REQUIRE_APPROVAL, "amount_over_threshold", "sanctioned_counterparty"
    )
    outcome = await engine.decide(freeze())
    assert (outcome.status, outcome.approval_id) == ("pending_approval", "apr_2")
    assert store.approvals["apr_1"].status is ApprovalStatus.EXPIRED
    assert store.approvals["apr_2"].reasons == ["amount_over_threshold", "sanctioned_counterparty"]
    expired = [r for r in store.audit if r.event_type == "approval_expired"]
    assert [(r.approval_id, r.reasons) for r in expired] == [("apr_1", ["amount_over_threshold"])]
    assert "approval_redeemed" not in [r.event_type for r in store.audit]
    assert first_invalid(store.audit) is None


@pytest.mark.parametrize(
    "reasons",
    [("counterparty_missing", "amount_over_threshold"), ("amount_over_threshold",)],
    ids=["same_reasons", "subset"],
)
async def test_approval_covering_current_reasons_is_redeemed(engine, store, policy, clock, reasons):
    policy.decision = policy_result(
        Decision.REQUIRE_APPROVAL, "amount_over_threshold", "counterparty_missing"
    )
    await engine.decide(freeze())
    await store.transition(
        "apr_1", from_statuses=(ApprovalStatus.PENDING,), to=ApprovalStatus.APPROVED, at=clock()
    )
    policy.decision = policy_result(Decision.REQUIRE_APPROVAL, *reasons)
    outcome = await engine.decide(freeze())
    assert outcome.status == "allowed"
    assert store.approvals["apr_1"].status is ApprovalStatus.USED


async def test_changed_arguments_need_new_approval(engine, store, policy, clock):
    policy.decision = NEEDS_APPROVAL
    await engine.decide(freeze(25000))
    await store.transition(
        "apr_1", from_statuses=(ApprovalStatus.PENDING,), to=ApprovalStatus.APPROVED, at=clock()
    )
    outcome = await engine.decide(freeze(26000))
    assert (outcome.status, outcome.approval_id) == ("pending_approval", "apr_2")
    assert store.approvals["apr_1"].status is ApprovalStatus.APPROVED


async def test_policy_deny_wins_over_existing_approval(engine, store, policy, clock):
    policy.decision = NEEDS_APPROVAL
    await engine.decide(freeze())
    await store.transition(
        "apr_1", from_statuses=(ApprovalStatus.PENDING,), to=ApprovalStatus.APPROVED, at=clock()
    )
    policy.decision = policy_result(Decision.DENY, "agent_suspended")
    outcome = await engine.decide(freeze())
    assert outcome.reasons == ["agent_suspended"]
    assert store.approvals["apr_1"].status is ApprovalStatus.APPROVED


async def test_expired_approval_is_replaced(engine, store, policy, clock):
    policy.decision = NEEDS_APPROVAL
    await engine.decide(freeze())
    clock.advance(hours=25)
    outcome = await engine.decide(freeze())
    assert outcome.approval_id == "apr_2"
    assert store.approvals["apr_1"].status is ApprovalStatus.EXPIRED
    assert "approval_expired" in [r.event_type for r in store.audit]


async def test_store_unavailable_denies(engine, store):
    store.fail = StoreUnavailable("down")
    outcome = await engine.decide(freeze(5000))
    assert outcome.reasons == ["governance_store_unavailable"]


async def test_audit_failure_denies(engine, store):
    store.fail = StoreError("insert failed")
    outcome = await engine.decide(freeze(5000))
    assert outcome.reasons == ["audit_write_failed"]


def interleaving_engine(clock, store, policy) -> GovernanceEngine:
    return GovernanceEngine(
        verifier=make_verifier(),
        policy=policy,
        store=store,
        approval_ttl=timedelta(hours=24),
        approver_role="compliance_supervisor",
        clock=clock,
        new_id=id_sequence(),
    )


async def test_concurrent_redemption_allows_once_and_shares_new_approval(clock, policy):
    store = InterleavingStore()
    engine = interleaving_engine(clock, store, policy)
    policy.decision = NEEDS_APPROVAL
    await engine.decide(freeze())
    await store.transition(
        "apr_1", from_statuses=(ApprovalStatus.PENDING,), to=ApprovalStatus.APPROVED, at=clock()
    )
    outcomes = await asyncio.gather(*(engine.decide(freeze()) for _ in range(5)))
    allowed = [o for o in outcomes if o.status == "allowed"]
    pending = [o for o in outcomes if o.status == "pending_approval"]
    assert len(allowed) == 1
    assert len(pending) == 4
    assert len({o.approval_id for o in pending}) == 1
    assert store.approvals["apr_1"].status is ApprovalStatus.USED
    assert first_invalid(store.audit) is None


async def test_concurrent_first_requests_share_one_approval(clock, policy):
    store = InterleavingStore()
    engine = interleaving_engine(clock, store, policy)
    policy.decision = NEEDS_APPROVAL
    outcomes = await asyncio.gather(*(engine.decide(freeze()) for _ in range(3)))
    assert {o.status for o in outcomes} == {"pending_approval"}
    assert len({o.approval_id for o in outcomes}) == 1
    assert len(store.approvals) == 1
    assert first_invalid(store.audit) is None


async def test_approval_conflict_exhaustion_denies_with_one_audit_event(clock, policy):
    store = AlwaysConflictStore()
    engine = interleaving_engine(clock, store, policy)
    policy.decision = NEEDS_APPROVAL
    outcome = await engine.decide(freeze())
    assert (outcome.status, outcome.reasons) == ("denied", ["approval_conflict"])
    [record] = store.audit
    assert (record.event_type, record.decision, record.reasons) == (
        "decision",
        "denied",
        ["approval_conflict"],
    )
