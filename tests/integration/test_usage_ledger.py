import asyncio
import os
import random
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
import pytest
from dotenv import load_dotenv
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

from ai_gov_gateway.engine.audit import first_invalid
from ai_gov_gateway.engine.ledger import (
    Admission,
    CallIdInUse,
    LedgerUnavailable,
    SettledRow,
    Settlement,
)
from ai_gov_gateway.engine.limits import (
    AdmissionSnapshot,
    LimitDenial,
    Overage,
    UsageLimits,
    usage_windows,
)
from ai_gov_gateway.engine.models import AuditEvent
from ai_gov_gateway.engine.postgres import USAGE_LOCK_SPACE, PostgresStore

pytestmark = pytest.mark.integration
load_dotenv()
URL = os.environ.get("GOV_TEST_DATABASE_URL", "")
TZ = ZoneInfo("Asia/Taipei")
NOW = datetime(2026, 11, 13, 10, tzinfo=TZ)
TTL = timedelta(seconds=300)
LIMITS = UsageLimits(
    tier="default",
    budget_usd={"day": Decimal("2.0"), "month": Decimal("20.0")},
    rpm=20,
    tpm=150_000,
    max_concurrent=4,
    max_input_chars=100_000,
    max_output_tokens=4096,
)
DENY = LimitDenial(status=429, code="user_rate_limited", limit={"type": "rpm"}, details={})


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
        await conn.execute("DROP TABLE IF EXISTS approvals, audit_events, llm_usage")
    await store.ensure_schema()
    return store


def _admission(
    call_id: str,
    *,
    now: datetime = NOW,
    owner: str = "owner-a",
    user_sub: str = "alice-sub",
    limits: UsageLimits = LIMITS,
) -> Admission:
    return Admission(
        owner=owner,
        call_id=call_id,
        agent_id="kyt-agent",
        key_alias="kyt-agent@1.6",
        user_sub=user_sub,
        model="gemini",
        limits=limits,
        windows=usage_windows(now, TZ),
        inflight_since=now - TTL,
        now=now,
    )


def _settlement(
    call_id: str,
    cost: str | Decimal,
    *,
    at: datetime = NOW + timedelta(seconds=1),
    owner: str = "owner-a",
    tokens: tuple[int, int] = (100, 50),
) -> Settlement:
    return Settlement(
        owner=owner,
        call_id=call_id,
        cost_usd=Decimal(cost),
        prompt_tokens=tokens[0],
        completion_tokens=tokens[1],
        at=at,
        settled_by="callback",
    )


def _allow(_: AdmissionSnapshot) -> LimitDenial | None:
    return None


def _audit(row: SettledRow, overage: Overage, settlement: Settlement) -> AuditEvent:
    return AuditEvent(
        event_type="llm_limit_exceeded",
        agent_id=row.agent_id,
        key_alias=row.key_alias,
        user_sub=row.user_sub,
        decision="over_limit",
        reasons=overage.exceeded,
        details={
            "call_id": row.call_id,
            "model": row.model,
            "tier": row.tier,
            "cost_usd": str(settlement.cost_usd),
            "over": overage.breakdown,
            "crossed": overage.crossed,
            "settled_by": settlement.settled_by,
        },
    )


async def _admit(store: PostgresStore, call_id: str, **kwargs: Any) -> None:
    assert await store.admit(_admission(call_id, **kwargs), _allow) is None


async def _seed(
    store: PostgresStore,
    call_id: str,
    cost: str,
    *,
    now: datetime = NOW,
    settled_at: datetime | None = None,
    tokens: tuple[int, int] = (100, 50),
    limits: UsageLimits = LIMITS,
) -> Overage | None:
    await _admit(store, call_id, now=now, limits=limits)
    at = settled_at or now + timedelta(seconds=1)
    return await store.settle(_settlement(call_id, cost, at=at, tokens=tokens), TZ, _audit)


async def _snapshot(store: PostgresStore, now: datetime = NOW) -> AdmissionSnapshot:
    seen: list[AdmissionSnapshot] = []

    def peek(snapshot: AdmissionSnapshot) -> LimitDenial:
        seen.append(snapshot)
        return DENY

    await store.admit(_admission("peek", now=now), peek)
    return seen[0]


async def _row(call_id: str, owner: str = "owner-a") -> dict[str, Any] | None:
    async with await psycopg.AsyncConnection.connect(URL, row_factory=dict_row) as conn:
        cursor = await conn.execute(
            "SELECT * FROM llm_usage WHERE owner = %s AND call_id = %s", (owner, call_id)
        )
        return await cursor.fetchone()


async def test_admit_inserts_in_flight(store):
    assert await store.admit(_admission("c1"), _allow) is None
    row = await _row("c1")
    assert (row["status"], row["tier"], row["agent_id"], row["key_alias"], row["user_sub"]) == (
        "in_flight",
        "default",
        "kyt-agent",
        "kyt-agent@1.6",
        "alice-sub",
    )
    assert UsageLimits.model_validate(row["limits"]) == LIMITS
    assert row["created_at"] == NOW


async def test_admit_skips_insert_when_denied(store):
    assert await store.admit(_admission("c1"), lambda _: DENY) == DENY
    assert await _row("c1") is None


async def test_snapshot_counts_only_settled_cost(store):
    await _seed(store, "c1", "0.01", now=NOW - timedelta(minutes=5))
    await _admit(store, "c2", now=NOW - timedelta(seconds=10))
    await _admit(store, "c3", now=NOW - timedelta(seconds=10))
    assert await store.release("owner-a", "c3") is True
    snapshot = await _snapshot(store)
    assert (snapshot.day_usd, snapshot.month_usd, snapshot.in_flight) == (
        Decimal("0.01"),
        Decimal("0.01"),
        1,
    )


async def test_snapshot_in_flight_ignores_stale(store):
    await _admit(store, "old", now=NOW - TTL - timedelta(seconds=1))
    await _admit(store, "new", now=NOW - timedelta(seconds=10))
    assert (await _snapshot(store)).in_flight == 1


async def test_snapshot_separates_day_and_month(store):
    await _seed(store, "last-month", "1", now=datetime(2026, 10, 31, 23, tzinfo=TZ))
    await _seed(store, "this-month", "0.1", now=datetime(2026, 11, 2, 9, tzinfo=TZ))
    await _seed(store, "today", "0.01", now=datetime(2026, 11, 13, 0, 5, tzinfo=TZ))
    snapshot = await _snapshot(store)
    assert (snapshot.day_usd, snapshot.month_usd) == (Decimal("0.01"), Decimal("0.11"))


async def test_snapshot_minute_windows(store):
    # rpm 以 created_at 計算，tpm 以 settled_at 計算
    await _seed(
        store,
        "created-old-settled-recent",
        "0.001",
        now=NOW - timedelta(seconds=90),
        settled_at=NOW - timedelta(seconds=30),
        tokens=(100, 50),
    )
    await _seed(
        store,
        "both-old",
        "0.001",
        now=NOW - timedelta(seconds=80),
        settled_at=NOW - timedelta(seconds=65),
        tokens=(900, 100),
    )
    await _admit(store, "recent", now=NOW - timedelta(seconds=20))
    snapshot = await _snapshot(store)
    assert (snapshot.minute_requests, snapshot.minute_oldest) == (1, NOW - timedelta(seconds=20))
    assert (snapshot.minute_tokens, snapshot.minute_oldest_settled) == (
        150,
        NOW - timedelta(seconds=30),
    )


async def test_minute_window_across_month_start(store):
    now = datetime(2026, 12, 1, 0, 0, 30, tzinfo=TZ)
    await _seed(
        store,
        "late",
        "0.5",
        now=datetime(2026, 11, 30, 23, 59, 50, tzinfo=TZ),
        settled_at=datetime(2026, 11, 30, 23, 59, 55, tzinfo=TZ),
    )
    snapshot = await _snapshot(store, now)
    assert (snapshot.minute_requests, snapshot.minute_tokens) == (1, 150)
    assert (snapshot.day_usd, snapshot.month_usd) == (Decimal("0"), Decimal("0"))


async def test_settle_records_cost_tokens_and_zero_overage(store):
    overage = await _seed(store, "c1", "0.012", tokens=(1000, 200))
    row = await _row("c1")
    assert overage is not None and overage.exceeded == []
    assert (row["status"], row["cost_usd"], row["prompt_tokens"], row["completion_tokens"]) == (
        "settled",
        Decimal("0.012"),
        1000,
        200,
    )
    assert (row["over_day_usd"], row["over_month_usd"], row["over_tpm_tokens"]) == (0, 0, 0)
    assert (row["settled_at"], row["settled_by"]) == (NOW + timedelta(seconds=1), "callback")
    assert await store.audit_records() == []


async def test_settle_crossing_writes_audit(store):
    await _seed(store, "c1", "1.99", now=NOW - timedelta(hours=1))
    overage = await _seed(store, "c2", "0.03")
    row = await _row("c2")
    records = await store.audit_records()
    assert overage is not None and overage.day == Decimal("0.02")
    assert row["over_day_usd"] == Decimal("0.02")
    assert [(r.event_type, r.key_alias) for r in records] == [
        ("llm_limit_exceeded", "kyt-agent@1.6")
    ]
    assert records[0].reasons == ["budget_day"]
    assert records[0].details["crossed"]["budget_day"] is True
    assert records[0].details["over"]["budget_day"]["over"] == "0.02"
    assert first_invalid(records) is None


async def test_settle_uses_admission_snapshot_limits(store):
    lowered = LIMITS.model_copy(update={"tier": "low", "budget_usd": {"day": Decimal("1.0")}})
    await _seed(store, "c0", "1.5", now=NOW - timedelta(hours=1), limits=lowered)
    await _admit(store, "c1")
    await _admit(store, "c2", limits=lowered)
    # c1 放行時日額度是 2.0：1.5 + 0.4 未超出；c2 以 1.0 計算
    first = await store.settle(_settlement("c1", "0.4"), TZ, _audit)
    second = await store.settle(_settlement("c2", "0.1"), TZ, _audit)
    assert first is not None and first.day == 0
    assert second is not None and second.day == Decimal("0.1")
    assert (await _row("c2"))["tier"] == "low"


async def test_settle_after_release_still_records(store):
    await _admit(store, "c1")
    await store.release("owner-a", "c1")
    assert await store.settle(_settlement("c1", "0.02"), TZ, _audit) is not None
    row = await _row("c1")
    assert (row["status"], row["cost_usd"]) == ("settled", Decimal("0.02"))


async def test_settle_twice_is_noop(store):
    await _seed(store, "c1", "0.02")
    before = await _row("c1")
    again = _settlement("c1", "5", at=NOW + timedelta(seconds=9))
    assert await store.settle(again, TZ, _audit) is None
    assert await _row("c1") == before
    assert await store.audit_records() == []


async def test_settle_unknown_call_returns_none(store):
    assert await store.settle(_settlement("missing", "0.1"), TZ, _audit) is None


async def test_release_does_not_override_settled(store):
    await _seed(store, "c1", "0.02")
    assert await store.release("owner-a", "c1") is False
    assert (await _row("c1"))["status"] == "settled"


async def test_concurrent_settlements_sum_exactly(store):
    await _seed(store, "c0", "1.90", now=NOW - timedelta(hours=1))
    rng = random.Random(7)
    costs = {f"c{n}": Decimal(f"{rng.uniform(0.01, 0.03):.4f}") for n in range(1, 21)}
    for call_id in costs:
        await _admit(store, call_id)
    results = await asyncio.gather(
        *(store.settle(_settlement(c, cost), TZ, _audit) for c, cost in costs.items())
    )
    overs = [r.day for r in results if r is not None]
    records = await store.audit_records()
    assert len(overs) == 20
    assert sum(overs) == max(Decimal("0"), Decimal("1.90") + sum(costs.values()) - 2)
    assert len([o for o in overs if o > 0]) == len(records)
    assert [r.details["crossed"]["budget_day"] for r in records].count(True) == 1
    assert first_invalid(records) is None


async def test_audit_failure_rolls_back_settlement(store):
    await _seed(store, "c1", "1.99", now=NOW - timedelta(hours=1))
    await _admit(store, "c2")

    def broken(row: SettledRow, overage: Overage, settlement: Settlement) -> AuditEvent:
        raise RuntimeError("audit builder failed")

    with pytest.raises(LedgerUnavailable):
        await store.settle(_settlement("c2", "0.03"), TZ, broken)
    row = await _row("c2")
    assert (row["status"], row["cost_usd"], row["over_day_usd"]) == ("in_flight", None, None)
    assert await store.audit_records() == []


async def test_different_users_do_not_block(store):
    async with await psycopg.AsyncConnection.connect(URL) as holder:
        await holder.execute(
            "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
            (USAGE_LOCK_SPACE, "kyt-agent:alice-sub"),
        )
        await asyncio.wait_for(store.admit(_admission("bob-1", user_sub="bob-sub"), _allow), 5)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(store.admit(_admission("alice-1"), _allow), 0.5)
    assert (await _row("bob-1"))["status"] == "in_flight"
    assert await _row("alice-1") is None


async def test_duplicate_call_id_raises(store):
    await _admit(store, "c1")
    with pytest.raises(CallIdInUse):
        await store.admit(_admission("c1"), _allow)
    await _admit(store, "c1", owner="owner-b")


async def test_stale_in_flight_lists_old_rows(store):
    await _admit(store, "released", now=NOW - timedelta(minutes=30))
    await store.release("owner-a", "released")
    await _seed(store, "settled", "0.01", now=NOW - timedelta(minutes=25))
    await _admit(store, "oldest", now=NOW - timedelta(minutes=20))
    await _admit(store, "old", owner="owner-b", now=NOW - timedelta(minutes=15))
    await _admit(store, "recent", now=NOW - timedelta(minutes=1))
    cutoff = NOW - timedelta(minutes=10)
    assert await store.stale_in_flight(cutoff, 10) == [("owner-a", "oldest"), ("owner-b", "old")]
    assert await store.stale_in_flight(cutoff, 1) == [("owner-a", "oldest")]


async def test_numeric_precision(store):
    precise = LIMITS.model_copy(update={"budget_usd": {"day": Decimal("0.123456789")}})
    await _seed(store, "c1", "0.000000375", limits=precise)
    row = await _row("c1")
    assert row["cost_usd"] == Decimal("0.000000375")
    assert UsageLimits.model_validate(row["limits"]).budget_usd["day"] == Decimal("0.123456789")


async def test_unavailable_database_raises():
    ledger = PostgresStore("postgresql://nobody@127.0.0.1:1/governance_test")
    with pytest.raises(LedgerUnavailable):
        await ledger.admit(_admission("c1"), _allow)
    with pytest.raises(LedgerUnavailable):
        await ledger.settle(_settlement("c1", "0.1"), TZ, _audit)
    with pytest.raises(LedgerUnavailable):
        await ledger.release("owner-a", "c1")
    with pytest.raises(LedgerUnavailable):
        await ledger.stale_in_flight(NOW, 10)
