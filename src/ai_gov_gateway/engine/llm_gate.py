"""LLM 路徑的依使用者限制：放行前的檢查（spec §4.3）、結算與釋放（spec §8.3）。"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from functools import partial
from typing import Any, NoReturn
from zoneinfo import ZoneInfo

from ai_gov_gateway.engine.clock import Clock, utc_now
from ai_gov_gateway.engine.fingerprint import parse_agent_key
from ai_gov_gateway.engine.guardrails.fields import text_length
from ai_gov_gateway.engine.guardrails.models import GuardrailConfig, GuardrailError
from ai_gov_gateway.engine.guardrails.pii import PseudonymMap
from ai_gov_gateway.engine.guardrails.service import LlmGuardrail, Message, access_event
from ai_gov_gateway.engine.ledger import (
    Admission,
    DuplicateCallId,
    LedgerUnavailable,
    SettledRow,
    Settlement,
    UsageLedger,
)
from ai_gov_gateway.engine.limits import Overage, UsageLimits, check_admission, usage_windows
from ai_gov_gateway.engine.models import AgentRef, AuditEvent, UserIdentity
from ai_gov_gateway.engine.store import GovernanceStore, StoreError, StoreUnavailable
from ai_gov_gateway.engine.tokens import TokenError, TokenVerifier

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LlmRequest:
    key_alias: str | None
    owner: str | None
    user_token: str | None = field(repr=False)
    end_user_override: str | None  # 自帶身份的 header 或欄位名稱
    model: str | None
    call_id: str | None
    messages: list[Message]
    max_tokens: object
    max_completion_tokens: object


@dataclass
class Admitted:
    messages: list[Message]
    pseudonyms: PseudonymMap
    max_tokens: int | None  # client 沒帶上限時要補上的值


@dataclass(frozen=True)
class _Caller:
    """拒絕稽核需要的身份；隨檢查的進度補上 user 與 OPA 的結果。"""

    request: LlmRequest
    agent: AgentRef | None
    user: UserIdentity | None = None
    config: GuardrailConfig | None = None


class LlmGate:
    def __init__(
        self,
        *,
        verifier: TokenVerifier,
        guardrail: LlmGuardrail,
        ledger: UsageLedger,
        store: GovernanceStore,
        priced: Callable[[], frozenset[str]],
        tz: ZoneInfo,
        inflight_ttl: timedelta,
        clock: Clock = utc_now,
    ) -> None:
        self._verifier = verifier
        self._guardrail = guardrail
        self._ledger = ledger
        self._store = store
        self._priced = priced
        self._tz = tz
        self._inflight_ttl = inflight_ttl
        self._clock = clock

    async def admit(self, request: LlmRequest) -> Admitted:
        """spec §4.3 的順序；一律 fail-closed，拒絕時寫 llm_access 後丟 GuardrailError。"""
        caller = _Caller(request, parse_agent_key(request.key_alias))
        if request.end_user_override:
            error = GuardrailError(400, "end_user_override", field=request.end_user_override)
            await self._deny(caller, error, {"field": request.end_user_override})
        user = await self._authenticate(caller)
        agent, config = await self._guardrail.resolve(
            request.key_alias, user, model=request.model, call_id=request.call_id
        )
        caller = _Caller(request, agent, user, config)
        if config.limits is None:
            await self._deny(caller, GuardrailError(403, "no_llm_quota"))
        limits = config.limits
        inject = await self._check_single_request(caller, limits)
        guarded = await self._guardrail.guard_request(
            agent=agent,
            config=config,
            model=request.model,
            call_id=request.call_id,
            messages=request.messages,
        )
        await self._admit_usage(caller, agent, user, limits)
        return Admitted(
            messages=guarded.messages,
            pseudonyms=guarded.pseudonyms,
            max_tokens=limits.max_output_tokens if inject else None,
        )

    async def settle(self, settlement: Settlement) -> bool:
        """回傳是否寫入帳本；回應已送出，無法再拒絕，遺失的結算由對帳補上。"""
        try:
            overage = await self._ledger.settle(settlement, self._tz, _limit_exceeded_event)
        except Exception as error:
            logger.error("llm usage settlement failed: %s", type(error).__name__)
            return False
        return overage is not None

    async def release(self, owner: str | None, call_id: object) -> bool:
        if owner is None or not isinstance(call_id, str) or not call_id:
            return False
        try:
            return await self._ledger.release(owner, call_id)
        except Exception as error:
            logger.error("llm usage release failed: %s", type(error).__name__)
            return False

    async def _authenticate(self, caller: _Caller) -> UserIdentity:
        token = caller.request.user_token
        if not token:
            await self._deny(caller, GuardrailError(401, "missing_user_token"))
        try:
            return await asyncio.to_thread(self._verifier.verify, token)
        except TokenError:
            await self._deny(caller, GuardrailError(401, "invalid_user_token"))

    async def _check_single_request(self, caller: _Caller, limits: UsageLimits) -> bool:
        """輸出與輸入的單次上限，回傳是否要補上 max_tokens。

        放在 guardrail 之前：被拒絕的請求不做 PII 偵測，也不留下 llm_guardrail 稽核。
        """
        request = caller.request
        values = {
            "max_tokens": request.max_tokens,
            "max_completion_tokens": request.max_completion_tokens,
        }
        cap = limits.max_output_tokens
        for name, value in values.items():
            if value is None:
                continue
            # bool 是 int 的子類別，True 不能當成 1
            if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= cap:
                error = GuardrailError(400, "max_tokens_exceeded", field=name)
                await self._deny(
                    caller,
                    error,
                    {"field": name, "limit": {"type": "max_output_tokens", "max": cap}},
                )
        chars = text_length(request.messages)
        if chars > limits.max_input_chars:
            limit = {"type": "max_input_chars", "max": limits.max_input_chars}
            await self._deny(
                caller, GuardrailError(400, "content_too_large"), {"limit": limit, "chars": chars}
            )
        return all(value is None for value in values.values())

    async def _admit_usage(
        self, caller: _Caller, agent: AgentRef, user: UserIdentity, limits: UsageLimits
    ) -> None:
        request, model = caller.request, caller.request.model
        if model is None or not self._is_priced(model):
            raise GuardrailError(503, "budget_unavailable")
        if request.owner is None or not isinstance(request.call_id, str) or not request.call_id:
            raise GuardrailError(503, "budget_unavailable")
        now = self._clock()
        admission = Admission(
            owner=request.owner,
            call_id=request.call_id,
            agent_id=agent.id,
            key_alias=agent.key_alias,
            user_sub=user.sub,
            model=model,
            limits=limits,
            windows=usage_windows(now, self._tz),
            inflight_since=now - self._inflight_ttl,
            now=now,
        )
        check = partial(check_admission, limits=limits, now=now, tz=self._tz)
        try:
            denial = await self._ledger.admit(admission, check)
        except (LedgerUnavailable, DuplicateCallId) as failure:
            raise GuardrailError(503, "budget_unavailable") from failure
        if denial is not None:
            rejection = GuardrailError(denial.status, denial.code, limit=denial.limit)
            await self._deny(caller, rejection, {"limit": denial.limit, **denial.details})

    def _is_priced(self, model: str) -> bool:
        # 單價表由 runtime 在第一次使用時建立；取不到時無法計算成本，一律拒絕
        try:
            return model in self._priced()
        except Exception as error:
            logger.error("llm pricing unavailable: %s", type(error).__name__)
            return False

    async def _deny(
        self, caller: _Caller, error: GuardrailError, details: dict[str, Any] | None = None
    ) -> NoReturn:
        event = _access_event(caller, error.code, details or {})
        try:
            await self._store.append_audit(event, self._clock())
        except (StoreUnavailable, StoreError) as failure:
            raise GuardrailError(503, "audit_write_failed") from failure
        raise error


def _access_event(caller: _Caller, code: str, extra: dict[str, Any]) -> AuditEvent:
    request, config = caller.request, caller.config
    details: dict[str, Any] = {"call_id": request.call_id, "model": request.model}
    if config is not None and config.limits is not None:
        details["tier"] = config.limits.tier
    return access_event(
        key_alias=request.key_alias,
        agent=caller.agent,
        user=caller.user,
        code=code,
        policy_version=config.policy_version if config else None,
        details={**details, **extra},
    )


def _limit_exceeded_event(row: SettledRow, overage: Overage, settlement: Settlement) -> AuditEvent:
    """結算時任一項超額（spec §10.2）；金額以字串記錄，hash 才不受 JSON 數值表示影響。"""
    agent = parse_agent_key(row.key_alias)
    return AuditEvent(
        event_type="llm_limit_exceeded",
        agent_id=row.agent_id,
        agent_version=agent.version if agent else None,
        key_alias=row.key_alias,
        user_sub=row.user_sub,
        decision="over_limit",
        reasons=overage.exceeded,
        details={
            "call_id": row.call_id,
            "model": row.model,
            "tier": row.tier,
            "cost_usd": str(settlement.cost_usd),
            "tokens": {
                "prompt": settlement.prompt_tokens,
                "completion": settlement.completion_tokens,
            },
            "over": overage.breakdown,
            "crossed": overage.crossed,
            "settled_by": settlement.settled_by,
        },
    )
