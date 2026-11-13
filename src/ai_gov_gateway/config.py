from zoneinfo import ZoneInfo

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """gateway 內治理元件的設定；容器以環境變數提供，不讀 .env。"""

    model_config = SettingsConfigDict(env_prefix="GOV_", extra="ignore")

    database_url: str = Field(default="", repr=False)
    opa_url: str = "http://opa:8181"
    opa_timeout_seconds: float = Field(default=2.0, gt=0)
    oidc_issuer: str = "http://localhost:8080/realms/governance"
    oidc_jwks_url: str = "http://keycloak:8080/realms/governance/protocol/openid-connect/certs"
    oidc_audience: str = "ai-gateway"
    # 只接受由使用者登入的 client 簽發的 token（azp）；環境變數以 JSON 陣列設定
    oidc_allowed_clients: list[str] = Field(default=["kyt-cli", "agw-cli"], min_length=1)
    approval_ttl_hours: int = Field(default=24, gt=0)
    approver_role: str = "compliance_supervisor"
    guardrail_rules_path: str = "/opt/ai-gov-gateway/guardrails/injection_rules.json"
    # 單則訊息與整個請求的掃描上限；超過時拒絕而不截斷，避免沒掃到的部分被放行
    guardrail_max_chars: int = Field(default=200_000, gt=0)
    guardrail_max_total_chars: int = Field(default=1_000_000, gt=0)
    # 個人預算的日、月以此時區切分
    budget_timezone: str = "Asia/Taipei"
    # 超過此時間仍未結算的 in_flight 不計入同時進行數，留給對帳處理
    usage_inflight_ttl_seconds: int = Field(default=300, gt=0)

    @field_validator("budget_timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        # ZoneInfoNotFoundError 是 KeyError，pydantic 只把 ValueError 當成驗證失敗
        try:
            ZoneInfo(value)
        except (KeyError, ValueError) as error:
            raise ValueError("unknown timezone") from error
        return value
