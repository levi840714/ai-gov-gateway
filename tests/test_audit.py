from datetime import UTC, datetime, timedelta, timezone

import pytest

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


def test_record_hash_golden_vector():
    event = AuditEvent(event_type="decision", tool="t", reasons=["r"], arguments={"n": 1})
    assert record_hash(GENESIS_HASH, datetime(2026, 1, 1, tzinfo=UTC), event) == (
        "befddbd42dbfb017825863da461eb480640e4ccbdb10f5b783bbd751950d261e"
    )


def test_naive_timestamp_is_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        record_hash(GENESIS_HASH, datetime(2026, 1, 1), _event(1))


def test_details_is_omitted_from_hash_when_unset():
    event = AuditEvent(event_type="decision", agent_id="kyt-agent", reasons=["x"])
    assert "details" not in event.model_dump(mode="json", exclude_none=True)


def test_llm_guardrail_event_with_details_verifies():
    ts = datetime(2026, 10, 23, 9, tzinfo=UTC)
    event = AuditEvent(
        event_type="llm_guardrail",
        agent_id="kyt-agent",
        key_alias="kyt-agent",
        decision="redact",
        reasons=["override.en"],
        details={"pii": {"EMAIL": 1}, "injection": [{"rule": "override.en", "tool": "t"}]},
    )
    record = build_record(record_id=1, prev_hash=GENESIS_HASH, ts=ts, event=event)
    assert first_invalid([record]) is None
    tampered = record.model_copy(update={"details": {"pii": {"EMAIL": 2}}})
    assert first_invalid([tampered]) == 1
