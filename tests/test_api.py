from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_gov_gateway.api.approvals import router
from ai_gov_gateway.engine.approvals import ApprovalService
from ai_gov_gateway.engine.engine import GovernanceEngine
from ai_gov_gateway.engine.guardrails.injection import RuleDetector
from ai_gov_gateway.engine.guardrails.service import LlmGuardrail
from ai_gov_gateway.engine.models import Decision, ToolCall
from ai_gov_gateway.engine.store import StoreError, StoreUnavailable
from ai_gov_gateway.runtime import Services, get_services
from tests.fakes import (
    FakeClock,
    FakeGuardrailPolicy,
    FakePiiDetector,
    FakePolicy,
    InMemoryStore,
    id_sequence,
    policy_result,
)
from tests.jwt_helpers import alice_token, bob_token, carol_token, make_token, make_verifier


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture
def services(store: InMemoryStore) -> Services:
    clock, verifier = FakeClock(), make_verifier()
    policy = FakePolicy(policy_result(Decision.REQUIRE_APPROVAL, "amount_over_threshold"))
    engine = GovernanceEngine(
        verifier=verifier,
        policy=policy,
        store=store,
        approval_ttl=timedelta(hours=24),
        approver_role="compliance_supervisor",
        clock=clock,
        new_id=id_sequence(),
    )
    approvals = ApprovalService(store=store, approver_role="compliance_supervisor", clock=clock)
    llm_guardrail = LlmGuardrail(
        policy=FakeGuardrailPolicy(),
        store=store,
        detector=FakePiiDetector(),
        rules=RuleDetector.from_file(Path("guardrails/injection_rules.json")),
        max_chars=1000,
    )
    return Services(
        verifier=verifier, engine=engine, approvals=approvals, llm_guardrail=llm_guardrail
    )


@pytest.fixture
def client(services) -> TestClient:
    app = FastAPI()
    app.include_router(router)

    async def override() -> Services:
        return services

    app.dependency_overrides[get_services] = override
    return TestClient(app)


async def _pending(services: Services) -> str:
    call = ToolCall(
        key_alias="kyt-agent@1.4",
        user_token=alice_token(),
        server="account_ops",
        tool="freeze_account",
        arguments={"account_id": "acct-001", "amount_usd": 25000, "counterparty": "0x1"},
    )
    outcome = await services.engine.decide(call)
    return outcome.approval_id


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_requires_token(client):
    assert client.get("/governance/approvals").status_code == 401
    response = client.get("/governance/approvals", headers=_auth("garbage"))
    assert (response.status_code, response.json()["detail"]) == (401, "invalid_token")


async def test_analyst_cannot_list(client, services):
    await _pending(services)
    response = client.get("/governance/approvals", headers=_auth(alice_token()))
    assert (response.status_code, response.json()["detail"]) == (403, "role_required")


async def test_supervisor_lists_and_approves(client, services):
    approval_id = await _pending(services)
    listed = client.get("/governance/approvals?status=pending", headers=_auth(bob_token()))
    assert [a["id"] for a in listed.json()] == [approval_id]
    detail = client.get(f"/governance/approvals/{approval_id}", headers=_auth(bob_token()))
    assert detail.json()["approval"]["arguments"]["amount_usd"] == 25000
    approved = client.post(
        f"/governance/approvals/{approval_id}/approve",
        json={"comment": "已確認"},
        headers=_auth(bob_token()),
    )
    assert (approved.status_code, approved.json()["status"]) == (200, "approved")
    again = client.post(
        f"/governance/approvals/{approval_id}/reject", json={}, headers=_auth(carol_token())
    )
    assert (again.status_code, again.json()["detail"]) == (409, "approval_not_pending")


async def test_not_found(client):
    response = client.get("/governance/approvals/apr_missing", headers=_auth(bob_token()))
    assert (response.status_code, response.json()["detail"]) == (404, "approval_not_found")


async def test_audit_verify(client, services):
    await _pending(services)
    response = client.get("/governance/audit/verify", headers=_auth(bob_token()))
    assert response.json() == {"records": 1, "first_invalid_id": None}


def _client_with_failing_services(error: Exception) -> TestClient:
    app = FastAPI()
    app.include_router(router)

    async def failing() -> Services:
        raise error

    app.dependency_overrides[get_services] = failing
    return TestClient(app)


def test_store_unavailable_when_building_services():
    failing = _client_with_failing_services(StoreUnavailable("down"))
    response = failing.get("/governance/approvals", headers=_auth(bob_token()))
    assert (response.status_code, response.json()["detail"]) == (
        503,
        "governance_store_unavailable",
    )


def test_missing_token_wins_over_service_failure():
    failing = _client_with_failing_services(StoreUnavailable("down"))
    response = failing.get("/governance/approvals")
    assert (response.status_code, response.json()["detail"]) == (401, "missing_token")


def test_store_error_when_building_services():
    failing = _client_with_failing_services(StoreError("boom"))
    response = failing.get("/governance/approvals", headers=_auth(bob_token()))
    assert (response.status_code, response.json()["detail"]) == (500, "governance_store_error")


def test_store_error_on_list_is_neutral_500(client, store):
    store.fail = StoreError("boom")
    response = client.get("/governance/approvals", headers=_auth(bob_token()))
    assert (response.status_code, response.json()["detail"]) == (500, "governance_store_error")


async def test_analyst_forbidden_on_audit_verify_and_approve(client, services):
    approval_id = await _pending(services)
    verify = client.get("/governance/audit/verify", headers=_auth(alice_token()))
    assert (verify.status_code, verify.json()["detail"]) == (403, "role_required")
    approve = client.post(
        f"/governance/approvals/{approval_id}/approve", json={}, headers=_auth(alice_token())
    )
    assert (approve.status_code, approve.json()["detail"]) == (403, "role_required")


def test_post_approve_requires_token(client):
    response = client.post("/governance/approvals/apr_1/approve", json={})
    assert (response.status_code, response.json()["detail"]) == (401, "missing_token")


def test_comment_too_long_is_rejected(client):
    response = client.post(
        "/governance/approvals/apr_1/approve",
        json={"comment": "x" * 2001},
        headers=_auth(bob_token()),
    )
    assert response.status_code == 422


async def test_delegator_reads_own_status(client, services):
    approval_id = await _pending(services)
    response = client.get(
        f"/governance/approvals/{approval_id}/status", headers=_auth(alice_token())
    )
    assert response.status_code == 200
    assert response.json()["status"] == "pending"
    assert "arguments" not in response.json()


async def test_status_of_others_is_not_found(client, services):
    approval_id = await _pending(services)
    dave = make_token("dave", ["analyst"])
    response = client.get(f"/governance/approvals/{approval_id}/status", headers=_auth(dave))
    assert (response.status_code, response.json()["detail"]) == (404, "approval_not_found")


def test_status_requires_token(client):
    assert client.get("/governance/approvals/apr_1/status").status_code == 401
