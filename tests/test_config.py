import os

import pytest
from pydantic import ValidationError

from ai_gov_gateway.config import Settings


def test_defaults_point_at_compose_services(monkeypatch):
    for key in [k for k in os.environ if k.startswith("GOV_")]:
        monkeypatch.delenv(key)
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


def test_rejects_non_positive_ttl(monkeypatch):
    monkeypatch.setenv("GOV_APPROVAL_TTL_HOURS", "-5")
    with pytest.raises(ValidationError):
        Settings()


def test_repr_hides_database_url(monkeypatch):
    monkeypatch.setenv("GOV_DATABASE_URL", "postgresql://u:secret@h/db")
    assert "secret" not in repr(Settings())
