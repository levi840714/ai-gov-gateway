"""LiteLLM 依 guardrail 設定載入此模組；載入時把治理路由掛到同一個 app。

只在 gateway 映像內使用（需要 LiteLLM），主機上的單元測試不 import。
"""

import logging
from typing import Any

from litellm.integrations.custom_guardrail import CustomGuardrail
from litellm.proxy.proxy_server import app
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from ai_gov_gateway.api.approvals import router
from ai_gov_gateway.engine.models import Outcome
from ai_gov_gateway.engine.store import StoreUnavailable
from ai_gov_gateway.hook_adapter import (
    has_ambiguous_authorization,
    is_mcp_path,
    tool_call_from_hook,
)
from ai_gov_gateway.runtime import get_services

logger = logging.getLogger(__name__)


class AuthorizationHeaderGuard:
    """在 LiteLLM 處理 MCP 請求前拒絕 Authorization-* header，見 has_ambiguous_authorization。"""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and is_mcp_path(scope["path"])
            and has_ambiguous_authorization(name.decode("latin-1") for name, _ in scope["headers"])
        ):
            response = JSONResponse({"detail": "ambiguous_authorization_headers"}, status_code=400)
            await response(scope, receive, send)
            return
        await self._app(scope, receive, send)


def _install_header_guard() -> None:
    # LiteLLM 在 lifespan 內才載入 guardrail，此時 middleware stack 已建好、不能再 add_middleware
    if app.middleware_stack is None:
        app.add_middleware(AuthorizationHeaderGuard)
    else:
        app.middleware_stack = AuthorizationHeaderGuard(app.middleware_stack)


if not getattr(app.state, "governance_routes", False):
    app.include_router(router)
    _install_header_guard()
    app.state.governance_routes = True


class GovernanceBlocked(ValueError):
    """訊息為 JSON 字串，會原樣出現在 client 的 tool result（isError=True）。"""


class GovernanceGuardrail(CustomGuardrail):
    async def async_pre_call_hook(
        self, user_api_key_dict: Any, cache: Any, data: dict[str, Any], call_type: str
    ) -> dict[str, Any]:
        if call_type != "call_mcp_tool":
            return data
        try:
            call = tool_call_from_hook(data, getattr(user_api_key_dict, "key_alias", None))
            outcome = await (await get_services()).engine.decide(call)
        except StoreUnavailable:
            logger.error("governance store unavailable")
            outcome = Outcome.denied(["governance_store_unavailable"])
        except Exception:
            logger.exception("governance decision failed")
            outcome = Outcome.denied(["governance_internal_error"])
        if outcome.status == "allowed":
            return data
        raise GovernanceBlocked(outcome.to_message())
