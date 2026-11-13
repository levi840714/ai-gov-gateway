"""對帳：把逾時的 in_flight 依 LiteLLM_SpendLogs 結算或釋放（spec §8.5）。"""

import argparse
import asyncio
import os
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol

import psycopg

from ai_gov_gateway.config import Settings
from ai_gov_gateway.engine.clock import Clock, utc_now
from ai_gov_gateway.engine.fingerprint import sha256_hex
from ai_gov_gateway.engine.ledger import LedgerUnavailable, Settlement, UsageLedger
from ai_gov_gateway.engine.llm_gate import LlmGate
from ai_gov_gateway.engine.postgres import PostgresStore
from ai_gov_gateway.runtime import build_services

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


@dataclass(frozen=True)
class SpendLog:
    status: str
    spend: float
    prompt_tokens: int
    completion_tokens: int
    api_key: str  # LiteLLM 存的 key 雜湊；owner 是它的 sha256
    end_time: datetime | None = None  # 請求結束時間（UTC）；缺漏時以對帳當下代替


class SpendLogReader(Protocol):
    async def find(self, call_id: str) -> Sequence[SpendLog]: ...


@dataclass
class ReconcileReport:
    settled: int = 0
    released: int = 0
    released_missing: int = 0
    unresolved: list[str] = field(default_factory=list)

    def render(self) -> str:
        line = (
            f"settled={self.settled} released={self.released} "
            f"released_missing={self.released_missing} unresolved={len(self.unresolved)}"
        )
        return "\n".join([line, *self.unresolved])


class PostgresSpendLogs:
    def __init__(self, database_url: str) -> None:
        self._url = database_url

    async def find(self, call_id: str) -> list[SpendLog]:
        query = (
            'SELECT status, spend, prompt_tokens, completion_tokens, api_key, "endTime" '
            'FROM "LiteLLM_SpendLogs" WHERE litellm_call_id = %s'
        )
        async with await psycopg.AsyncConnection.connect(self._url) as conn:
            cursor = await conn.execute(query, (call_id,))
            rows = await cursor.fetchall()
        return [
            SpendLog(str(r[0]), float(r[1]), int(r[2]), int(r[3]), str(r[4]), _utc(r[5]))
            for r in rows
        ]


def _utc(value: datetime | None) -> datetime | None:
    # Prisma 的 DateTime 是不含時區的 UTC
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def reconcile(
    ledger: UsageLedger,
    spend_logs: SpendLogReader,
    gate: LlmGate,
    *,
    older_than: datetime,
    release_missing_before: datetime,
    batch: int = 500,
    clock: Clock = utc_now,
) -> ReconcileReport:
    """把逾時的 in_flight 依 LiteLLM_SpendLogs 結算或釋放；結算走與 callback 相同的超額計算。"""
    report = ReconcileReport()
    stale = await ledger.stale_in_flight(older_than, batch)
    # 沒有紀錄的放行要等更久才釋放，避免 SpendLogs 延遲寫入時被誤放
    missing_ok = set(await ledger.stale_in_flight(release_missing_before, batch))
    for owner, call_id in stale:
        logs = [log for log in await spend_logs.find(call_id) if sha256_hex(log.api_key) == owner]
        success = next((log for log in logs if log.status == "success"), None)
        if success is not None:
            settlement = Settlement(
                owner=owner,
                call_id=call_id,
                cost_usd=Decimal(str(round(success.spend, 12))),
                prompt_tokens=success.prompt_tokens,
                completion_tokens=success.completion_tokens,
                # 用請求的結束時間，舊用量才不會落進目前的 tpm 視窗；沒有 endTime 時退回現在
                at=success.end_time or clock(),
                settled_by="reconcile",
            )
            if await gate.settle(settlement):
                report.settled += 1
            else:
                report.unresolved.append(call_id)
        elif logs:
            report.released += await gate.release(owner, call_id)
        elif (owner, call_id) in missing_ok:
            # rate limit fallback 的第二個 call_id 永遠沒有 SpendLogs（spike B5）
            report.released_missing += await gate.release(owner, call_id)
        else:
            report.unresolved.append(call_id)
    return report


def parse_duration(text: str) -> timedelta:
    match = re.fullmatch(r"(\d+)([smhd])", text)
    if match is None:
        raise argparse.ArgumentTypeError(f"invalid duration: {text!r} (examples: 30s, 10m, 1h)")
    return timedelta(seconds=int(match.group(1)) * _UNITS[match.group(2)])


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m ai_gov_gateway.reconcile")
    parser.add_argument("--older-than", type=parse_duration, default=parse_duration("10m"))
    parser.add_argument(
        "--release-missing-after", type=parse_duration, default=parse_duration("1h")
    )
    parser.add_argument("--batch", type=int, default=500)
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> ReconcileReport:
    settings = Settings()
    services = await build_services(settings, lambda: frozenset())
    now = utc_now()
    return await reconcile(
        PostgresStore(settings.database_url),
        PostgresSpendLogs(os.environ["DATABASE_URL"]),
        services.llm_gate,
        older_than=now - args.older_than,
        release_missing_before=now - args.release_missing_after,
        batch=args.batch,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        report = asyncio.run(_run(args))
    except (psycopg.Error, LedgerUnavailable, KeyError, OSError) as error:
        print(f"database unavailable: {type(error).__name__}", file=sys.stderr)
        return 1
    print(report.render())
    return 0


if __name__ == "__main__":
    sys.exit(main())
