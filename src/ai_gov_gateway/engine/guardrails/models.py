from typing import Any, Literal

from pydantic import BaseModel, Field

from ai_gov_gateway.engine.limits import UsageLimits

InjectionAction = Literal["redact", "deny", "flag"]
AgentStatus = Literal["active", "suspended", "unregistered"]
PiiEntity = Literal["TW_ID", "TW_ARC", "PHONE", "EMAIL", "CREDIT_CARD", "BANK_ACCOUNT", "API_KEY"]
Exclusion = Literal["eth_address", "eth_tx_hash"]


class PiiSettings(BaseModel):
    entities: list[PiiEntity]
    exclude: list[Exclusion] = Field(default_factory=list)


class InjectionSettings(BaseModel):
    action: InjectionAction
    system_notice: bool


class GuardrailConfig(BaseModel):
    """OPA governance.llm 的結果；欄位缺漏或名稱不支援時驗證失敗，視為 policy_error。"""

    agent_status: AgentStatus
    pii: PiiSettings
    injection: InjectionSettings
    high_risk_tools: list[str] = Field(default_factory=list)
    limits: UsageLimits | None
    policy_version: str


class InjectionFinding(BaseModel):
    rule: str
    tool: str
    tool_call_id: str | None
    content_sha256: str


class PiiSpan(BaseModel):
    entity: str
    start: int
    end: int


class GuardrailError(Exception):
    """以 HTTP 狀態碼與 governance JSON 回給 client；內容只含代碼。"""

    def __init__(
        self,
        status: int,
        code: str,
        rules: list[str] | None = None,
        field: str | None = None,
        limit: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.rules = rules or []
        self.field = field
        self.limit = limit

    def body(self) -> dict[str, Any]:
        body: dict[str, Any] = {"governance": "denied", "reasons": [self.code]}
        if self.rules:
            body["rules"] = self.rules
        if self.field:
            body["field"] = self.field
        if self.limit:
            body["limit"] = self.limit
        return body
