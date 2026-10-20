from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_gov_gateway.api.approvals import router
from ai_gov_gateway.engine.approvals import ApprovalService
from ai_gov_gateway.engine.engine import GovernanceEngine
from ai_gov_gateway.engine.models import Decision, ToolCall
from ai_gov_gateway.runtime import Services, get_services
from tests.fakes import FakeClock, FakePolicy, InMemoryStore, id_sequence, policy_result
from tests.jwt_helpers import alice_token, bob_token, carol_token, make_verifier


@pytest.fixture
def services() -> Services:
    clock, store, verifier = FakeClock(), InMemoryStore(), make_verifier()
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
    return Services(verifier=verifier, engine=engine, approvals=approvals)


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
