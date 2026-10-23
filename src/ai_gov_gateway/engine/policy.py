from typing import Any, Protocol

import httpx

from ai_gov_gateway.engine.guardrails.models import GuardrailConfig
from ai_gov_gateway.engine.models import PolicyDecision


class PolicyError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class PolicyClient(Protocol):
    async def evaluate(self, policy_input: dict[str, Any]) -> PolicyDecision: ...


class OpaPolicyClient:
    DECISION_PATH = "/v1/data/governance/decision"
    LLM_PATH = "/v1/data/governance/llm"

    def __init__(
        self, base_url: str, timeout: float, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._base_url = base_url
        self._timeout = timeout
        self._transport = transport

    async def evaluate(self, policy_input: dict[str, Any]) -> PolicyDecision:
        result = await self._query(self.DECISION_PATH, policy_input)
        try:
            return PolicyDecision.model_validate(result)
        except ValueError as error:
            raise PolicyError("policy_error") from error

    async def llm_config(self, agent_id: str) -> GuardrailConfig:
        result = await self._query(self.LLM_PATH, {"agent": {"id": agent_id}})
        try:
            return GuardrailConfig.model_validate(result)
        except ValueError as error:
            raise PolicyError("policy_error") from error

    async def _query(self, path: str, policy_input: dict[str, Any]) -> Any:
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url, timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.post(path, json={"input": policy_input})
                response.raise_for_status()
        except httpx.HTTPError as error:
            raise PolicyError("policy_unavailable") from error
        except (TypeError, ValueError) as error:
            # httpx 序列化 input 失敗（NaN、datetime 等）屬輸入問題，不是 OPA 不可用
            raise PolicyError("policy_error") from error
        try:
            body = response.json()
        except ValueError as error:
            raise PolicyError("policy_error") from error
        return body.get("result") if isinstance(body, dict) else None
