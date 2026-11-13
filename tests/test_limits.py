from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from ai_gov_gateway.engine.limits import (
    AdmissionSnapshot,
    SettleContext,
    UsageLimits,
    check_admission,
    compute_overage,
    overage,
    period_bounds,
    usage_windows,
)

VALID = {
    "tier": "default",
    "role": "*",
    "budget_usd": {"day": 2.0},
    "rpm": 20,
    "tpm": 150000,
    "max_concurrent": 4,
    "max_input_chars": 100000,
    "max_output_tokens": 4096,
}


def test_limits_parse_decimal_budget():
    assert UsageLimits.model_validate(VALID).budget_usd == {"day": Decimal("2.0")}


def test_limits_reject_unknown_period():
    with pytest.raises(ValidationError):
        UsageLimits.model_validate({**VALID, "budget_usd": {"week": 1}})


@pytest.mark.parametrize("field", ["rpm", "tpm", "max_concurrent", "max_input_chars"])
def test_limits_reject_non_positive_counts(field):
    with pytest.raises(ValidationError):
        UsageLimits.model_validate({**VALID, field: 0})


def test_limits_reject_empty_budget():
    with pytest.raises(ValidationError):
        UsageLimits.model_validate({**VALID, "budget_usd": {}})


TZ = ZoneInfo("Asia/Taipei")
NOW = datetime(2026, 11, 13, 15, 0, tzinfo=UTC)  # 台北 23:00
LIMITS = UsageLimits.model_validate({**VALID, "budget_usd": {"day": 2.0, "month": 20.0}})


def snapshot(**overrides):
    base = {
        "day_usd": "0",
        "month_usd": "0",
        "in_flight": 0,
        "minute_requests": 0,
        "minute_oldest": None,
        "minute_tokens": 0,
        "minute_oldest_settled": None,
    }
    return AdmissionSnapshot.model_validate({**base, **overrides})


def test_windows_use_local_calendar():
    windows = usage_windows(NOW, TZ)
    assert windows.day_start == datetime(2026, 11, 13, 0, 0, tzinfo=TZ)
    assert windows.month_start == datetime(2026, 11, 1, 0, 0, tzinfo=TZ)
    assert windows.minute_start == NOW - timedelta(seconds=60)


def test_window_after_local_midnight():
    windows = usage_windows(datetime(2026, 11, 13, 16, 30, tzinfo=UTC), TZ)  # 台北 11/14 00:30
    assert windows.day_start == datetime(2026, 11, 14, 0, 0, tzinfo=TZ)


def test_period_bounds_end_at_next_local_period():
    bounds = period_bounds(datetime(2026, 12, 31, 16, 30, tzinfo=UTC), TZ)  # 台北 2027/1/1 00:30
    assert bounds["day"] == (
        datetime(2027, 1, 1, tzinfo=TZ),
        datetime(2027, 1, 2, tzinfo=TZ),
    )
    assert bounds["month"] == (datetime(2027, 1, 1, tzinfo=TZ), datetime(2027, 2, 1, tzinfo=TZ))


def test_admit_when_below_limits():
    assert check_admission(snapshot(day_usd="1.99"), LIMITS, NOW, TZ) is None


def test_deny_when_day_budget_reached():
    denial = check_admission(snapshot(day_usd="2.0"), LIMITS, NOW, TZ)
    assert denial is not None
    assert (denial.status, denial.code) == (403, "user_budget_exceeded")
    assert denial.limit == {"type": "budget_day", "resets_at": "2026-11-14T00:00:00+08:00"}
    assert denial.details == {"settled_usd": "2.0", "limit_usd": "2.0"}


def test_month_budget_checked():
    denial = check_admission(snapshot(day_usd="1", month_usd="20"), LIMITS, NOW, TZ)
    assert denial is not None
    assert denial.limit == {"type": "budget_month", "resets_at": "2026-12-01T00:00:00+08:00"}
    assert denial.details == {"settled_usd": "20", "limit_usd": "20.0"}


def test_month_reset_rolls_over_year():
    december = datetime(2026, 12, 20, 4, 0, tzinfo=UTC)
    denial = check_admission(snapshot(month_usd="20"), LIMITS, december, TZ)
    assert denial is not None
    assert denial.limit["resets_at"] == "2027-01-01T00:00:00+08:00"


def test_missing_period_is_skipped():
    day_only = UsageLimits.model_validate(VALID)
    assert check_admission(snapshot(month_usd="999"), day_only, NOW, TZ) is None


def test_concurrency_limit():
    denial = check_admission(snapshot(in_flight=4), LIMITS, NOW, TZ)
    assert denial is not None
    assert (denial.status, denial.code, denial.limit) == (
        429,
        "user_rate_limited",
        {"type": "concurrency", "retry_after_seconds": 1},
    )


def test_rpm_limit_returns_retry_after():
    oldest = NOW - timedelta(seconds=48)
    denial = check_admission(snapshot(minute_requests=20, minute_oldest=oldest), LIMITS, NOW, TZ)
    assert denial is not None
    assert denial.limit == {"type": "rpm", "retry_after_seconds": 12}
    assert denial.details == {"requests": 20, "limit": 20}


def test_retry_after_is_at_least_one_second():
    oldest = NOW - timedelta(seconds=60)
    denial = check_admission(snapshot(minute_requests=20, minute_oldest=oldest), LIMITS, NOW, TZ)
    assert denial is not None
    assert denial.limit["retry_after_seconds"] == 1


def test_tpm_counts_settled_tokens_only():
    oldest = NOW - timedelta(seconds=30)
    assert check_admission(snapshot(minute_tokens=149_999), LIMITS, NOW, TZ) is None
    denial = check_admission(
        snapshot(minute_tokens=150_000, minute_oldest_settled=oldest), LIMITS, NOW, TZ
    )
    assert denial is not None
    assert denial.limit == {"type": "tpm", "retry_after_seconds": 30}


def test_rate_limits_checked_before_budget():
    # 兩者都超過時回 429（可重試），避免短時間大量請求都被記成預算用完
    denial = check_admission(snapshot(day_usd="5", in_flight=4), LIMITS, NOW, TZ)
    assert denial is not None
    assert denial.status == 429


def test_overage_zero_below_limit():
    assert overage(before=Decimal("1.0"), cost=Decimal("0.5"), limit=Decimal("2.0")) == Decimal("0")


def test_overage_exactly_reaching_limit_is_zero():
    got = overage(before=Decimal("1.99"), cost=Decimal("0.01"), limit=Decimal("2.0"))
    assert got == Decimal("0")


def test_overage_of_crossing_request():
    got = overage(before=Decimal("1.99"), cost=Decimal("0.03"), limit=Decimal("2.0"))
    assert got == Decimal("0.02")


def test_overage_after_limit_is_full_cost():
    got = overage(before=Decimal("2.01"), cost=Decimal("0.03"), limit=Decimal("2.0"))
    assert got == Decimal("0.03")


@given(st.lists(st.decimals(min_value=0, max_value=1, places=6), min_size=1, max_size=30))
def test_overage_sum_equals_total_excess(costs):
    # 依序結算時，各筆超額加總等於總成本超出上限的量（spec §8.4）
    limit, before, total_over = Decimal("2.0"), Decimal("1.5"), Decimal("0")
    for cost in costs:
        total_over += overage(before=before, cost=cost, limit=limit)
        before += cost
    assert total_over == max(Decimal("0"), before - limit)


def test_settle_context_reports_each_exceeded_item():
    context = SettleContext(
        limits=LIMITS,
        cost=Decimal("0.03"),
        tokens=1200,
        day_before=Decimal("1.99"),
        month_before=Decimal("5"),
        minute_tokens_before=149_500,
    )
    result = compute_overage(context)
    assert result.day == Decimal("0.02")
    assert result.month == Decimal("0")
    assert result.tpm_tokens == 700
    assert result.exceeded == ["budget_day", "tpm"]
    assert result.crossed == {"budget_day": True, "tpm": True}
    assert result.breakdown["budget_day"] == {
        "limit": "2.0",
        "before": "1.99",
        "after": "2.02",
        "over": "0.02",
    }
    assert result.breakdown["tpm"]["over"] == "700"


def test_settle_context_skips_period_without_limit_and_marks_already_over():
    day_only = UsageLimits.model_validate(VALID)
    context = SettleContext(
        limits=day_only,
        cost=Decimal("0.5"),
        tokens=10,
        day_before=Decimal("3"),
        month_before=Decimal("999"),
        minute_tokens_before=0,
    )
    result = compute_overage(context)
    assert result.month == Decimal("0")
    assert result.exceeded == ["budget_day"]
    assert result.crossed == {"budget_day": False}
