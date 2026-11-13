import math
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

Period = Literal["day", "month"]


class UsageLimits(BaseModel):
    """OPA governance.llm 的 limits；欄位缺漏時驗證失敗，由 policy client 視為 policy_error。"""

    tier: str
    budget_usd: dict[Period, Decimal] = Field(min_length=1)
    rpm: int = Field(gt=0)
    tpm: int = Field(gt=0)
    max_concurrent: int = Field(gt=0)
    max_input_chars: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)


class UsageWindows(BaseModel):
    day_start: datetime
    month_start: datetime
    minute_start: datetime


class AdmissionSnapshot(BaseModel):
    """放行時讀出的用量；金額與 token 只含已結算的請求。"""

    day_usd: Decimal
    month_usd: Decimal
    in_flight: int
    minute_requests: int
    minute_oldest: datetime | None
    minute_tokens: int
    minute_oldest_settled: datetime | None


class LimitDenial(BaseModel):
    status: int
    code: str
    limit: dict[str, Any]
    details: dict[str, Any]


def _local_midnight(day: date, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, time(), tzinfo=tz)


def _next_month_start(day: date) -> date:
    return date(day.year + day.month // 12, day.month % 12 + 1, 1)


def usage_windows(now: datetime, tz: ZoneInfo) -> UsageWindows:
    local = now.astimezone(tz)
    return UsageWindows(
        day_start=_local_midnight(local.date(), tz),
        month_start=_local_midnight(local.date().replace(day=1), tz),
        minute_start=now - timedelta(seconds=60),
    )


def _retry_after(oldest: datetime | None, now: datetime) -> int:
    if oldest is None:
        return 1
    return max(1, math.ceil((oldest + timedelta(seconds=60) - now).total_seconds()))


def _rate_denial(kind: str, retry_after: int, details: dict[str, Any]) -> LimitDenial:
    return LimitDenial(
        status=429,
        code="user_rate_limited",
        limit={"type": kind, "retry_after_seconds": retry_after},
        details=details,
    )


def _budget_denial(
    period: Period, settled: Decimal, limit: Decimal, resets_at: datetime
) -> LimitDenial:
    return LimitDenial(
        status=403,
        code="user_budget_exceeded",
        limit={"type": f"budget_{period}", "resets_at": resets_at.isoformat()},
        details={"settled_usd": str(settled), "limit_usd": str(limit)},
    )


def check_admission(
    snapshot: AdmissionSnapshot, limits: UsageLimits, now: datetime, tz: ZoneInfo
) -> LimitDenial | None:
    """依序檢查同時進行數、rpm、tpm、日、月；已結算達到上限才拒絕。"""
    if snapshot.in_flight >= limits.max_concurrent:
        return _rate_denial(
            "concurrency", 1, {"in_flight": snapshot.in_flight, "limit": limits.max_concurrent}
        )
    if snapshot.minute_requests >= limits.rpm:
        return _rate_denial(
            "rpm",
            _retry_after(snapshot.minute_oldest, now),
            {"requests": snapshot.minute_requests, "limit": limits.rpm},
        )
    if snapshot.minute_tokens >= limits.tpm:
        return _rate_denial(
            "tpm",
            _retry_after(snapshot.minute_oldest_settled, now),
            {"tokens": snapshot.minute_tokens, "limit": limits.tpm},
        )
    local_today = now.astimezone(tz).date()
    periods: list[tuple[Period, Decimal, date]] = [
        ("day", snapshot.day_usd, local_today + timedelta(days=1)),
        ("month", snapshot.month_usd, _next_month_start(local_today)),
    ]
    for period, settled, resets_on in periods:
        limit = limits.budget_usd.get(period)
        if limit is not None and settled >= limit:
            return _budget_denial(period, settled, limit, _local_midnight(resets_on, tz))
    return None


def overage(*, before: Decimal, cost: Decimal, limit: Decimal) -> Decimal:
    """這一筆超出上限的部分：只算 limit 以上、且在 before 之後的成本。"""
    return max(Decimal("0"), before + cost - max(before, limit))


class SettleContext(BaseModel):
    limits: UsageLimits
    cost: Decimal
    tokens: int
    day_before: Decimal
    month_before: Decimal
    minute_tokens_before: int


class Overage(BaseModel):
    day: Decimal
    month: Decimal
    tpm_tokens: int
    exceeded: list[str]
    crossed: dict[str, bool]
    breakdown: dict[str, dict[str, str]]  # 稽核的 details.over


def compute_overage(context: SettleContext) -> Overage:
    limits = context.limits
    zero = Decimal("0")
    # (項目, 結算前, 增量, 上限)；沒有設定上限的期別不計算
    items: list[tuple[str, Decimal, Decimal, Decimal | None]] = [
        ("budget_day", context.day_before, context.cost, limits.budget_usd.get("day")),
        ("budget_month", context.month_before, context.cost, limits.budget_usd.get("month")),
        (
            "tpm",
            Decimal(context.minute_tokens_before),
            Decimal(context.tokens),
            Decimal(limits.tpm),
        ),
    ]
    overs: dict[str, Decimal] = {}
    exceeded: list[str] = []
    crossed: dict[str, bool] = {}
    breakdown: dict[str, dict[str, str]] = {}
    for name, before, delta, limit in items:
        if limit is None:
            overs[name] = zero
            continue
        over = overage(before=before, cost=delta, limit=limit)
        overs[name] = over
        if over > 0:
            exceeded.append(name)
            crossed[name] = before < limit
            breakdown[name] = {
                "limit": str(limit),
                "before": str(before),
                "after": str(before + delta),
                "over": str(over),
            }
    return Overage(
        day=overs["budget_day"],
        month=overs["budget_month"],
        tpm_tokens=int(overs["tpm"]),
        exceeded=exceeded,
        crossed=crossed,
        breakdown=breakdown,
    )
