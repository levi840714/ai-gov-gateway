"""LiteLLM 依 guardrail 設定載入此模組；載入時把治理路由掛到同一個 app。

只在 gateway 映像內使用（需要 LiteLLM），主機上的單元測試不 import。
"""

import logging
from typing import Any

from litellm.integrations.custom_guardrail import CustomGuardrail
from litellm.proxy.proxy_server import app

from ai_gov_gateway.api.approvals import router
from ai_gov_gateway.engine.models import Outcome
from ai_gov_gateway.hook_adapter import tool_call_from_hook
from ai_gov_gateway.runtime import get_services

logger = logging.getLogger(__name__)

if not getattr(app.state, "governance_routes", False):
    app.include_router(router)
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
        except Exception:
            logger.exception("governance decision failed")
            outcome = Outcome.denied(["governance_internal_error"])
        if outcome.status == "allowed":
            return data
        raise GovernanceBlocked(outcome.to_message())
