from typing import Any, Protocol

import httpx

from ai_gov_gateway.engine.models import PolicyDecision


class PolicyError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class PolicyClient(Protocol):
    async def evaluate(self, policy_input: dict[str, Any]) -> PolicyDecision: ...


class OpaPolicyClient:
    DECISION_PATH = "/v1/data/governance/decision"

    def __init__(
        self, base_url: str, timeout: float, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._base_url = base_url
        self._timeout = timeout
        self._transport = transport

    async def evaluate(self, policy_input: dict[str, Any]) -> PolicyDecision:
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url, timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.post(self.DECISION_PATH, json={"input": policy_input})
                response.raise_for_status()
        except httpx.HTTPError as error:
            raise PolicyError("policy_unavailable") from error
        try:
            body = response.json()
            result = body.get("result") if isinstance(body, dict) else None
            return PolicyDecision.model_validate(result)
        except ValueError as error:
            raise PolicyError("policy_error") from error
