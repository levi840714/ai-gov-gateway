from pydantic import Field
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
    approval_ttl_hours: int = Field(default=24, gt=0)
    approver_role: str = "compliance_supervisor"
