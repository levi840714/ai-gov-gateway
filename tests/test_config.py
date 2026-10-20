from ai_gov_gateway.config import Settings


def test_defaults_point_at_compose_services(monkeypatch):
    for key in ("GOV_OPA_URL", "GOV_APPROVAL_TTL_HOURS", "GOV_OIDC_AUDIENCE"):
        monkeypatch.delenv(key, raising=False)
    settings = Settings()
    assert settings.opa_url == "http://opa:8181"
    assert settings.approval_ttl_hours == 24
    assert settings.oidc_audience == "ai-gateway"
    assert settings.approver_role == "compliance_supervisor"


def test_reads_gov_prefixed_environment(monkeypatch):
    monkeypatch.setenv("GOV_OPA_URL", "http://localhost:8181")
    monkeypatch.setenv("GOV_APPROVAL_TTL_HOURS", "2")
    settings = Settings()
    assert settings.opa_url == "http://localhost:8181"
    assert settings.approval_ttl_hours == 2
