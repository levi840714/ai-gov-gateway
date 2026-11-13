from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Literal, Protocol
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from ai_gov_gateway.engine.limits import (
    AdmissionSnapshot,
    LimitDenial,
    Overage,
    UsageLimits,
    UsageWindows,
)
from ai_gov_gateway.engine.models import AuditEvent


class LedgerUnavailable(Exception):
    """帳本無法讀寫；pre-call 一律拒絕（budget_unavailable）。"""


class DuplicateCallId(Exception):
    """同一把 key 重複使用 call_id；call_id 可由 client 以 x-litellm-call-id 指定。"""


class Admission(BaseModel):
    owner: str
    call_id: str
    agent_id: str
    user_sub: str
    model: str
    limits: UsageLimits
    windows: UsageWindows
    inflight_since: datetime  # now − GOV_USAGE_INFLIGHT_TTL_SECONDS
    now: datetime


class Settlement(BaseModel):
    owner: str
    call_id: str
    cost_usd: Decimal
    prompt_tokens: int
    completion_tokens: int
    at: datetime
    settled_by: Literal["callback", "reconcile"]


class SettledRow(BaseModel):
    """結算後的帳本列；給稽核事件使用。"""

    agent_id: str
    user_sub: str
    model: str
    tier: str
    call_id: str
    created_at: datetime


AuditBuilder = Callable[[SettledRow, Overage, Settlement], AuditEvent]


class UsageLedger(Protocol):
    async def admit(
        self, admission: Admission, check: Callable[[AdmissionSnapshot], LimitDenial | None]
    ) -> LimitDenial | None:
        """在同一個交易中讀出用量、呼叫 check；沒有拒絕才寫入 in_flight。"""
        ...

    async def settle(
        self, settlement: Settlement, tz: ZoneInfo, build_audit: AuditBuilder
    ) -> Overage | None:
        """計算超額、更新帳本；有超額時同一個交易寫入稽核。找不到或已結算時回傳 None。"""
        ...

    async def release(self, owner: str, call_id: str) -> bool:
        """只把 in_flight 改為 released；回傳是否有改變。"""
        ...

    async def stale_in_flight(self, older_than: datetime, limit: int) -> list[tuple[str, str]]:
        """給對帳使用：(owner, call_id)，最早放行的在前。"""
        ...
