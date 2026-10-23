import asyncio
import os
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from dotenv import load_dotenv
from psycopg.conninfo import conninfo_to_dict

from ai_gov_gateway.engine.audit import first_invalid
from ai_gov_gateway.engine.models import (
    AgentRef,
    Approval,
    ApprovalStatus,
    AuditEvent,
    UserIdentity,
)
from ai_gov_gateway.engine.postgres import PostgresStore
from ai_gov_gateway.engine.store import ApprovalConflict, StoreError, StoreUnavailable

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
    # fixture 會 DROP TABLE，只在名稱以 _test 結尾的資料庫上執行
    if not str(conninfo_to_dict(URL).get("dbname", "")).endswith("_test"):
        pytest.skip("GOV_TEST_DATABASE_URL must point at a database whose name ends with _test")
    store = PostgresStore(URL)
    await store.ensure_schema()
    async with await psycopg.AsyncConnection.connect(URL) as conn:
        await conn.execute("DROP TABLE IF EXISTS approvals, audit_events")
    await store.ensure_schema()
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


async def test_audit_hash_survives_json_number_edge_cases(store):
    for args in ({"v": 1e22}, {"v": -0.0}, {"v": 1.2345e300}, {"v": "a\u0000b"}):
        await store.append_audit(AuditEvent(event_type="decision", arguments=args), NOW)
    records = await store.audit_records()
    assert [r.arguments for r in records] == [
        {"v": 1e22},
        {"v": -0.0},
        {"v": 1.2345e300},
        {"v": "a\u0000b"},
    ]
    assert first_invalid(records) is None


async def test_ensure_schema_rejects_url_without_dbname():
    with pytest.raises(StoreError, match="no dbname"):
        await PostgresStore("postgresql://nobody:x@127.0.0.1:1").ensure_schema()


async def test_concurrent_audit_appends_keep_one_chain(store):
    await asyncio.gather(
        *(
            store.append_audit(AuditEvent(event_type="decision", arguments={"n": n}), NOW)
            for n in range(20)
        )
    )
    records = await store.audit_records()
    assert [r.id for r in records] == list(range(1, 21))
    assert first_invalid(records) is None


async def test_concurrent_approve_and_reject_has_one_winner(store):
    await store.create_approval(
        _approval("apr_1"), AuditEvent(event_type="approval_requested", approval_id="apr_1")
    )
    outcomes = await asyncio.gather(
        *(
            store.transition(
                "apr_1",
                from_statuses=(ApprovalStatus.PENDING,),
                to=to,
                at=NOW,
                actor=BOB,
                audit=AuditEvent(event_type=event, approval_id="apr_1"),
            )
            for to, event in (
                (ApprovalStatus.APPROVED, "approval_approved"),
                (ApprovalStatus.REJECTED, "approval_rejected"),
            )
        )
    )
    decided = [r for r in await store.audit_records() if r.event_type != "approval_requested"]
    assert sorted(outcomes) == [False, True]
    assert len(decided) == 1


async def test_concurrent_creates_for_one_fingerprint_have_one_winner(store):
    results = await asyncio.gather(
        *(
            store.create_approval(
                _approval(f"apr_{n}"),
                AuditEvent(event_type="approval_requested", approval_id=f"apr_{n}"),
            )
            for n in range(5)
        ),
        return_exceptions=True,
    )
    conflicts = [r for r in results if isinstance(r, ApprovalConflict)]
    assert len(conflicts) == 4
    assert len(await store.audit_records()) == 1


async def test_llm_guardrail_details_survive_round_trip(store):
    event = AuditEvent(
        event_type="llm_guardrail",
        agent_id="kyt-agent",
        decision="flag",
        reasons=["hidden.chars"],
        details={"pii": {"TW_ID": 1}, "rules_version": "2026-10-23.1", "big": 1e22},
    )
    await store.append_audit(event, NOW)
    records = await store.audit_records()
    assert records[-1].details == {
        "pii": {"TW_ID": 1},
        "rules_version": "2026-10-23.1",
        "big": 1e22,
    }
    assert first_invalid(records) is None


async def test_ensure_schema_adds_details_to_existing_table(store):
    async with await psycopg.AsyncConnection.connect(URL, autocommit=True) as conn:
        await conn.execute("ALTER TABLE audit_events DROP COLUMN IF EXISTS details")
    await store.ensure_schema()
    await store.append_audit(AuditEvent(event_type="llm_guardrail", details={"a": 1}), NOW)
    assert (await store.audit_records())[-1].details == {"a": 1}
