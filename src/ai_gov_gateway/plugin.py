"""LiteLLM 依 guardrail 設定載入此模組；載入時把治理路由掛到同一個 app。

只在 gateway 映像內使用（需要 LiteLLM），主機上的單元測試不 import。
"""

import logging
from collections import OrderedDict
from collections.abc import AsyncIterator
from typing import Any

from fastapi import HTTPException
from litellm.integrations.custom_guardrail import CustomGuardrail
from litellm.proxy.proxy_server import app
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from ai_gov_gateway.api.approvals import router
from ai_gov_gateway.engine.guardrails.models import GuardrailError
from ai_gov_gateway.engine.guardrails.pii import PseudonymMap
from ai_gov_gateway.engine.models import Outcome
from ai_gov_gateway.engine.store import StoreUnavailable
from ai_gov_gateway.hook_adapter import (
    has_ambiguous_authorization,
    restore_response,
    restore_stream,
    tool_call_from_hook,
)
from ai_gov_gateway.runtime import get_services

logger = logging.getLogger(__name__)


class AuthorizationHeaderGuard:
    """在 LiteLLM 路由前拒絕 Authorization-* header，見 has_ambiguous_authorization。

    Responses、chat completions、Anthropic messages 也會執行 MCP tool，因此套用到所有路由。
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and has_ambiguous_authorization(
            name.decode("latin-1") for name, _ in scope["headers"]
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


# docs/spikes/m4-findings.md A1：chat（含 streaming）一律是 acompletion；embeddings 是 aembedding
CHAT_CALL_TYPES = frozenset({"acompletion"})
MCP_CALL_TYPE = "call_mcp_tool"
# 對照表只放在這個 process 的記憶體；放進 data 的 metadata 會被 LiteLLM 的 callback 記錄
_PSEUDONYMS: OrderedDict[str, PseudonymMap] = OrderedDict()
# rate limit 觸發的 fallback 會留下收不到後續 hook 的 call_id，靠數量上限回收
_MAX_PENDING = 1000


def _remember(call_id: str, pseudonyms: PseudonymMap) -> None:
    _PSEUDONYMS[call_id] = pseudonyms
    while len(_PSEUDONYMS) > _MAX_PENDING:
        _PSEUDONYMS.popitem(last=False)


def _forget(call_id: object) -> PseudonymMap | None:
    return _PSEUDONYMS.pop(call_id, None) if isinstance(call_id, str) else None


def _reject(status: int, code: str) -> HTTPException:
    return HTTPException(status_code=status, detail=GuardrailError(status, code).body())


class LlmGuardrailHook(CustomGuardrail):
    async def async_pre_call_hook(
        self, user_api_key_dict: Any, cache: Any, data: dict[str, Any], call_type: str
    ) -> dict[str, Any]:
        # v1.105.1 的 MCP 不經過 pre_call；若之後的版本改變，交給 GovernanceGuardrail 處理
        if call_type == MCP_CALL_TYPE:
            return data
        if call_type not in CHAT_CALL_TYPES:
            raise _reject(400, "call_type_not_governed")
        call_id = data.get("litellm_call_id")
        try:
            guardrail = (await get_services()).llm_guardrail
            guarded = await guardrail.guard_request(
                key_alias=getattr(user_api_key_dict, "key_alias", None),
                model=data.get("model"),
                call_id=call_id,
                messages=data.get("messages") or [],
            )
        except GuardrailError as error:
            raise HTTPException(status_code=error.status, detail=error.body()) from None
        except Exception as error:
            # 只記錄例外類別：訊息可能含使用者內容
            logger.error("llm guardrail failed: %s", type(error).__name__)
            raise _reject(503, "guardrail_unavailable") from None
        if guarded.pseudonyms:
            if not isinstance(call_id, str) or not call_id:
                raise _reject(503, "guardrail_unavailable")
            _remember(call_id, guarded.pseudonyms)
        data["messages"] = guarded.messages
        return data

    async def async_post_call_success_hook(
        self, data: dict[str, Any], user_api_key_dict: Any, response: Any
    ) -> Any:
        pseudonyms = _forget(data.get("litellm_call_id"))
        if not pseudonyms:
            return response
        try:
            restore_response(response, pseudonyms)
        except Exception as error:
            logger.error("llm guardrail restore failed: %s", type(error).__name__)
            raise _reject(502, "guardrail_output_error") from None
        return response

    async def async_post_call_failure_hook(
        self,
        request_data: dict[str, Any],
        original_exception: Exception,
        user_api_key_dict: Any,
        traceback_str: str | None = None,
    ) -> None:
        # MCP 被治理擋下時也會呼叫；call_id 不在表中就什麼都不做
        _forget(request_data.get("litellm_call_id"))

    async def async_post_call_streaming_iterator_hook(
        self, user_api_key_dict: Any, response: Any, request_data: dict[str, Any]
    ) -> AsyncIterator[Any]:
        # streaming 不會呼叫 success hook，對照表在這裡取出
        pseudonyms = _forget(request_data.get("litellm_call_id"))
        if not pseudonyms:
            async for chunk in response:
                yield chunk
            return
        async for chunk in restore_stream(response, pseudonyms):
            yield chunk
