import os
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from dotenv import load_dotenv

from ai_gov_gateway.engine.audit import first_invalid
from ai_gov_gateway.engine.models import (
    AgentRef,
    Approval,
    ApprovalStatus,
    AuditEvent,
    UserIdentity,
)
from ai_gov_gateway.engine.postgres import PostgresStore
from ai_gov_gateway.engine.store import ApprovalConflict, StoreUnavailable

pytestmark = pytest.mark.integration
load_dotenv()
URL = os.environ.get("GOV_TEST_DATABASE_URL", "")
NOW = datetime(2026, 10, 20, 9, tzinfo=UTC)
BOB = UserIdentity(sub="bob-sub", username="bob", roles=["compliance_supervisor"])


def _approval(approval_id: str, fingerprint: str = "fp") -> Approval:
    return Approval(
        id=approval_id,
        fingerprint=fingerprint,
        status=ApprovalStatus.PENDING,
        agent=AgentRef(id="kyt-agent", version="1.4", key_alias="kyt-agent@1.4"),
        user_sub="alice-sub",
        username="alice",
        server="account_ops",
        tool="freeze_account",
        arguments={"amount_usd": 25000, "account_id": "acct-001"},
        reasons=["amount_over_threshold"],
        approver_role="compliance_supervisor",
        created_at=NOW,
        expires_at=NOW + timedelta(hours=24),
    )


@pytest.fixture
async def store() -> PostgresStore:
    if not URL:
        pytest.skip("GOV_TEST_DATABASE_URL not set")
    store = PostgresStore(URL)
    await store.ensure_schema()
    async with await psycopg.AsyncConnection.connect(URL) as conn:
        await conn.execute("TRUNCATE approvals, audit_events")
    return store


async def test_ensure_schema_is_idempotent(store):
    await store.ensure_schema()


async def test_approval_round_trip_and_transition(store):
    await store.create_approval(_approval("apr_1"), AuditEvent(event_type="approval_requested"))
    assert (await store.open_approval("fp")).id == "apr_1"
    changed = await store.transition(
        "apr_1",
        from_statuses=(ApprovalStatus.PENDING,),
        to=ApprovalStatus.APPROVED,
        at=NOW + timedelta(minutes=5),
        actor=BOB,
        comment="ok",
        audit=AuditEvent(event_type="approval_approved", actor_sub="bob-sub"),
    )
    audit_count = len(await store.audit_records())
    again = await store.transition(
        "apr_1",
        from_statuses=(ApprovalStatus.PENDING,),
        to=ApprovalStatus.REJECTED,
        at=NOW,
        audit=AuditEvent(event_type="approval_rejected"),
    )
    approval = await store.get_approval("apr_1")
    assert (changed, again) == (True, False)
    assert len(await store.audit_records()) == audit_count == 2
    assert (approval.status, approval.decided_by_username, approval.comment) == (
        ApprovalStatus.APPROVED,
        "bob",
        "ok",
    )
    assert approval.arguments == {"amount_usd": 25000, "account_id": "acct-001"}
    assert [a.id for a in await store.list_approvals(ApprovalStatus.APPROVED)] == ["apr_1"]


async def test_second_open_approval_for_same_fingerprint_conflicts(store):
    await store.create_approval(
        _approval("apr_1"), AuditEvent(event_type="approval_requested", approval_id="apr_1")
    )
    with pytest.raises(ApprovalConflict):
        await store.create_approval(
            _approval("apr_2"), AuditEvent(event_type="approval_requested", approval_id="apr_2")
        )
    records = await store.audit_records()
    assert [r.approval_id for r in records] == ["apr_1"]
    assert await store.get_approval("apr_2") is None

    await store.transition(
        "apr_1", from_statuses=(ApprovalStatus.PENDING,), to=ApprovalStatus.REJECTED, at=NOW
    )
    await store.create_approval(_approval("apr_3"), AuditEvent(event_type="approval_requested"))
    assert (await store.open_approval("fp")).id == "apr_3"


async def test_audit_chain_survives_round_trip(store):
    for n in range(3):
        await store.append_audit(
            AuditEvent(event_type="decision", arguments={"n": n, "amount_usd": 1.5}, reasons=["r"]),
            NOW + timedelta(seconds=n),
        )
    records = await store.audit_records()
    assert [r.id for r in records] == [1, 2, 3]
    assert first_invalid(records) is None


async def test_unreachable_database_raises_store_unavailable():
    store = PostgresStore("postgresql://nobody:x@127.0.0.1:1/none")
    with pytest.raises(StoreUnavailable):
        await store.audit_records()
