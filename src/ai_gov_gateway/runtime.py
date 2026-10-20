import asyncio
from dataclasses import dataclass
from datetime import timedelta

from ai_gov_gateway.config import Settings
from ai_gov_gateway.engine.approvals import ApprovalService
from ai_gov_gateway.engine.engine import GovernanceEngine
from ai_gov_gateway.engine.policy import OpaPolicyClient
from ai_gov_gateway.engine.postgres import PostgresStore
from ai_gov_gateway.engine.tokens import TokenVerifier, jwks_resolver


@dataclass(frozen=True)
class Services:
    verifier: TokenVerifier
    engine: GovernanceEngine
    approvals: ApprovalService


_services: Services | None = None
_lock = asyncio.Lock()


async def build_services(settings: Settings) -> Services:
    store = PostgresStore(settings.database_url)
    await store.ensure_schema()
    verifier = TokenVerifier(
        issuer=settings.oidc_issuer,
        audience=settings.oidc_audience,
        allowed_clients=settings.oidc_allowed_clients,
        key_resolver=jwks_resolver(settings.oidc_jwks_url),
    )
    engine = GovernanceEngine(
        verifier=verifier,
        policy=OpaPolicyClient(settings.opa_url, settings.opa_timeout_seconds),
        store=store,
        approval_ttl=timedelta(hours=settings.approval_ttl_hours),
        approver_role=settings.approver_role,
    )
    approvals = ApprovalService(store=store, approver_role=settings.approver_role)
    return Services(verifier=verifier, engine=engine, approvals=approvals)


async def get_services() -> Services:
    """第一次使用時才連線建立；失敗時下次呼叫會重試。"""
    global _services
    if _services is None:
        async with _lock:
            if _services is None:
                _services = await build_services(Settings())
    return _services
