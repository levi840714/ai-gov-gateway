import argparse
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from ai_gov_gateway.engine.fingerprint import sha256_hex
from ai_gov_gateway.engine.guardrails.injection import RuleDetector
from ai_gov_gateway.engine.guardrails.service import LlmGuardrail
from ai_gov_gateway.engine.ledger import Admission
from ai_gov_gateway.engine.limits import usage_windows
from ai_gov_gateway.engine.llm_gate import LlmGate
from ai_gov_gateway.reconcile import SpendLog, parse_duration, reconcile
from tests.fakes import (
    FakeClock,
    FakeGuardrailPolicy,
    FakePiiDetector,
    InMemoryLedger,
    InMemoryStore,
    UsageRow,
)
from tests.test_llm_gate import ALICE, KEY, MODEL, TTL, TZ, FakeTokenVerifier

API_KEY = "hashed-key-1"
OWNER = sha256_hex(API_KEY)
OLDER = timedelta(minutes=10)
MISSING = timedelta(hours=1)


class FakeSpendLogs:
    def __init__(self, logs: dict[str, list[SpendLog]] | None = None) -> None:
        self.logs = logs or {}

    async def find(self, call_id: str) -> list[SpendLog]:
        return self.logs.get(call_id, [])


def _log(
    status: str = "success",
    spend: float = 0.5,
    api_key: str = API_KEY,
    end_time: datetime | None = None,
) -> SpendLog:
    return SpendLog(status, spend, 100, 20, api_key, end_time)


class Harness:
    def __init__(self) -> None:
        self.clock = FakeClock()
        self.store = InMemoryStore()
        self.ledger = InMemoryLedger(self.store)
        self.policy = FakeGuardrailPolicy()
        guardrail = LlmGuardrail(
            policy=self.policy,
            store=self.store,
            detector=FakePiiDetector(),
            rules=RuleDetector.from_file(Path("guardrails/injection_rules.json")),
            max_chars=100_000,
            clock=self.clock,
        )
        self.gate = LlmGate(
            verifier=FakeTokenVerifier(),
            guardrail=guardrail,
            ledger=self.ledger,
            store=self.store,
            priced=lambda: frozenset({MODEL}),
            tz=TZ,
            inflight_ttl=TTL,
            clock=self.clock,
        )

    def seed(self, call_id: str, *, ago: timedelta) -> None:
        created = self.clock.now - ago
        admission = Admission(
            owner=OWNER,
            call_id=call_id,
            agent_id="kyt-agent",
            key_alias=KEY,
            user_sub=ALICE.sub,
            model=MODEL,
            limits=self.policy.config.limits,
            windows=usage_windows(created, TZ),
            inflight_since=created - TTL,
            now=created,
        )
        self.ledger.rows[(OWNER, call_id)] = UsageRow(admission=admission)

    async def run(self, logs: FakeSpendLogs):
        now = self.clock.now
        return await reconcile(
            self.ledger,
            logs,
            self.gate,
            older_than=now - OLDER,
            release_missing_before=now - MISSING,
            clock=self.clock,
        )


async def test_success_log_settles_with_reconcile_tag():
    h = Harness()
    h.seed("c1", ago=timedelta(minutes=30))
    report = await h.run(FakeSpendLogs({"c1": [_log(spend=3.0)]}))
    row = h.ledger.rows[(OWNER, "c1")]
    assert (report.settled, report.unresolved) == (1, [])
    assert (row.status, row.cost, row.tokens) == ("settled", Decimal("3.0"), 120)
    event = next(e for e in h.store.audit if e.event_type == "llm_limit_exceeded")
    assert event.details["settled_by"] == "reconcile"


async def test_settlement_uses_the_logs_end_time():
    h = Harness()
    h.seed("c1", ago=timedelta(minutes=30))
    ended = h.clock.now - timedelta(minutes=25)
    await h.run(FakeSpendLogs({"c1": [_log(end_time=ended)]}))
    assert h.ledger.rows[(OWNER, "c1")].settled_at == ended


async def test_settlement_falls_back_to_now_without_end_time():
    h = Harness()
    h.seed("c1", ago=timedelta(minutes=30))
    await h.run(FakeSpendLogs({"c1": [_log()]}))
    assert h.ledger.rows[(OWNER, "c1")].settled_at == h.clock.now


async def test_failure_only_releases():
    h = Harness()
    h.seed("c1", ago=timedelta(minutes=30))
    report = await h.run(FakeSpendLogs({"c1": [_log("failure")]}))
    assert (report.released, report.released_missing, report.settled) == (1, 0, 0)
    assert h.ledger.rows[(OWNER, "c1")].status == "released"


async def test_missing_log_kept_before_threshold():
    h = Harness()
    h.seed("c1", ago=timedelta(minutes=30))
    report = await h.run(FakeSpendLogs())
    assert report.unresolved == ["c1"]
    assert h.ledger.rows[(OWNER, "c1")].status == "in_flight"


async def test_missing_log_released_after_threshold():
    h = Harness()
    h.seed("c1", ago=timedelta(hours=2))
    report = await h.run(FakeSpendLogs())
    assert (report.released_missing, report.released, report.unresolved) == (1, 0, [])
    assert h.ledger.rows[(OWNER, "c1")].status == "released"


async def test_recent_rows_are_left_alone():
    h = Harness()
    h.seed("c1", ago=timedelta(minutes=1))
    report = await h.run(FakeSpendLogs({"c1": [_log()]}))
    assert (report.settled, report.unresolved) == (0, [])


async def test_owner_matches_hashed_api_key():
    h = Harness()
    h.seed("c1", ago=timedelta(minutes=30))
    report = await h.run(FakeSpendLogs({"c1": [_log(api_key="someone-else")]}))
    assert (report.settled, report.unresolved) == (0, ["c1"])
    assert h.ledger.rows[(OWNER, "c1")].status == "in_flight"


async def test_settle_failure_is_unresolved():
    h = Harness()
    h.seed("c1", ago=timedelta(minutes=30))

    async def broken(*args, **kwargs):
        raise RuntimeError("db down")

    h.ledger.settle = broken
    report = await h.run(FakeSpendLogs({"c1": [_log()]}))
    assert (report.settled, report.unresolved) == (0, ["c1"])


async def test_output_has_no_user_identifiers():
    h = Harness()
    h.seed("c1", ago=timedelta(minutes=30))
    h.seed("c2", ago=timedelta(hours=2))
    report = await h.run(FakeSpendLogs({"c2": []}))
    text = report.render()
    assert text.splitlines() == ["settled=0 released=0 released_missing=1 unresolved=1", "c1"]
    for secret in (OWNER, API_KEY, ALICE.sub, KEY):
        assert secret not in text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0s", timedelta(0)),
        ("30s", timedelta(seconds=30)),
        ("10m", timedelta(minutes=10)),
        ("1h", timedelta(hours=1)),
        ("2d", timedelta(days=2)),
    ],
)
def test_parse_duration(text, expected):
    assert parse_duration(text) == expected


@pytest.mark.parametrize("text", ["", "10", "m", "1.5h", "-1m", "1w", "10 m"])
def test_parse_duration_rejects(text):
    with pytest.raises(argparse.ArgumentTypeError):
        parse_duration(text)
