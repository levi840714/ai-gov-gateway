from datetime import UTC, datetime, timedelta, timezone

from ai_gov_gateway.engine.audit import (
    GENESIS_HASH,
    approval_event,
    build_record,
    first_invalid,
    record_hash,
)
from ai_gov_gateway.engine.models import (
    AgentRef,
    Approval,
    ApprovalStatus,
    AuditEvent,
    UserIdentity,
)

START = datetime(2026, 10, 20, 9, tzinfo=UTC)


def _event(n: int) -> AuditEvent:
    return AuditEvent(event_type="decision", tool="t", reasons=[f"r{n}"], arguments={"n": n})


def _chain(count: int):
    records, prev = [], GENESIS_HASH
    for n in range(1, count + 1):
        record = build_record(
            record_id=n, prev_hash=prev, ts=START + timedelta(seconds=n), event=_event(n)
        )
        records.append(record)
        prev = record.hash
    return records


def test_valid_chain_has_no_invalid_record():
    assert first_invalid(_chain(3)) is None


def test_tampered_field_is_reported():
    records = _chain(3)
    records[1] = records[1].model_copy(update={"username": "mallory"})
    assert first_invalid(records) == 2


def test_broken_link_is_reported():
    records = _chain(3)
    records[2] = records[2].model_copy(update={"prev_hash": GENESIS_HASH})
    assert first_invalid(records) == 3


def test_hash_is_independent_of_timezone_representation():
    taipei = START.astimezone(timezone(timedelta(hours=8)))
    assert record_hash(GENESIS_HASH, START, _event(1)) == record_hash(
        GENESIS_HASH, taipei, _event(1)
    )


def test_approval_event_copies_approval_fields():
    approval = Approval(
        id="apr_1",
        fingerprint="f",
        status=ApprovalStatus.APPROVED,
        agent=AgentRef(id="kyt-agent", version="1.4", key_alias="kyt-agent@1.4"),
        user_sub="alice-sub",
        username="alice",
        server="account_ops",
        tool="freeze_account",
        arguments={"amount_usd": 25000},
        reasons=["amount_over_threshold"],
        approver_role="compliance_supervisor",
        created_at=START,
        expires_at=START + timedelta(hours=24),
    )
    bob = UserIdentity(sub="bob-sub", username="bob", roles=["compliance_supervisor"])
    event = approval_event(
        "approval_approved", approval, actor=bob, decision="approved", comment="ok"
    )
    assert event.agent_version == "1.4"
    assert event.actor_username == "bob"
    assert event.approval_id == "apr_1"
    assert event.comment == "ok"
    assert event.arguments == {"amount_usd": 25000}
