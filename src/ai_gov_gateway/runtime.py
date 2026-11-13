import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from ai_gov_gateway.config import Settings
from ai_gov_gateway.engine.approvals import ApprovalService
from ai_gov_gateway.engine.engine import GovernanceEngine
from ai_gov_gateway.engine.guardrails.injection import RuleDetector
from ai_gov_gateway.engine.guardrails.pii import PresidioDetector
from ai_gov_gateway.engine.guardrails.service import LlmGuardrail
from ai_gov_gateway.engine.llm_gate import LlmGate
from ai_gov_gateway.engine.policy import OpaPolicyClient
from ai_gov_gateway.engine.postgres import PostgresStore
from ai_gov_gateway.engine.tokens import TokenVerifier, jwks_resolver


@dataclass(frozen=True)
class Services:
    verifier: TokenVerifier
    engine: GovernanceEngine
    approvals: ApprovalService
    llm_gate: LlmGate


Pricing = Callable[[], frozenset[str]]


def _unconfigured_pricing() -> frozenset[str]:
    raise RuntimeError("pricing source not configured")


_services: Services | None = None
_pricing: Pricing = _unconfigured_pricing
_lock = asyncio.Lock()


def use_pricing(source: Pricing) -> None:
    """有單價的模型別名來源；由 plugin 在載入時提供，runtime 才不必 import LiteLLM。"""
    global _pricing
    _pricing = source


async def build_services(settings: Settings, priced: Pricing) -> Services:
    store = PostgresStore(settings.database_url)
    await store.ensure_schema()
    verifier = TokenVerifier(
        issuer=settings.oidc_issuer,
        audience=settings.oidc_audience,
        allowed_clients=settings.oidc_allowed_clients,
        key_resolver=jwks_resolver(settings.oidc_jwks_url),
    )
    policy = OpaPolicyClient(settings.opa_url, settings.opa_timeout_seconds)
    engine = GovernanceEngine(
        verifier=verifier,
        policy=policy,
        store=store,
        approval_ttl=timedelta(hours=settings.approval_ttl_hours),
        approver_role=settings.approver_role,
    )
    approvals = ApprovalService(store=store, approver_role=settings.approver_role)
    llm_guardrail = LlmGuardrail(
        policy=policy,
        store=store,
        detector=PresidioDetector(),
        rules=RuleDetector.from_file(Path(settings.guardrail_rules_path)),
        max_chars=settings.guardrail_max_chars,
        max_total_chars=settings.guardrail_max_total_chars,
    )
    # 帳本與稽核共用同一個 store：結算的超額稽核與帳本在同一個交易寫入
    llm_gate = LlmGate(
        verifier=verifier,
        guardrail=llm_guardrail,
        ledger=store,
        store=store,
        priced=priced,
        tz=ZoneInfo(settings.budget_timezone),
        inflight_ttl=timedelta(seconds=settings.usage_inflight_ttl_seconds),
    )
    return Services(verifier=verifier, engine=engine, approvals=approvals, llm_gate=llm_gate)


async def get_services() -> Services:
    """第一次使用時才連線建立；失敗時下次呼叫會重試。"""
    global _services
    if _services is None:
        async with _lock:
            if _services is None:
                _services = await build_services(Settings(), lambda: _pricing())
    return _services
