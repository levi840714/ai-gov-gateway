import json

import httpx
import pytest
from typer.testing import CliRunner

from ai_gov_gateway import cli
from ai_gov_gateway.cli_auth import save_token

runner = CliRunner()
APPROVAL = {
    "id": "apr_1",
    "status": "pending",
    "server": "account_ops",
    "tool": "freeze_account",
    "username": "alice",
    "reasons": ["amount_over_threshold"],
    "expires_at": "2026-10-21T09:00:00Z",
}


@pytest.fixture
def gateway(monkeypatch, tmp_path):
    token_path = tmp_path / "token.json"
    monkeypatch.setenv("AGW_TOKEN_PATH", str(token_path))
    save_token(token_path, "bob-token")
    seen: list[httpx.Request] = []
    responses: dict[tuple[str, str], httpx.Response] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return responses.get((request.method, request.url.path), httpx.Response(404, json={}))

    monkeypatch.setattr(
        cli, "make_client", lambda: httpx.Client(transport=httpx.MockTransport(handler))
    )
    return seen, responses


def test_list_pending(gateway):
    seen, responses = gateway
    responses[("GET", "/governance/approvals")] = httpx.Response(200, json=[APPROVAL])
    result = runner.invoke(cli.app, ["approvals", "list"])
    assert result.exit_code == 0, result.output
    assert "apr_1" in result.output and "account_ops.freeze_account" in result.output
    assert seen[0].url.params["status"] == "pending"
    assert seen[0].headers["Authorization"] == "Bearer bob-token"


def test_approve_sends_comment(gateway):
    seen, responses = gateway
    responses[("POST", "/governance/approvals/apr_1/approve")] = httpx.Response(
        200, json=APPROVAL | {"status": "approved"}
    )
    result = runner.invoke(cli.app, ["approvals", "approve", "apr_1", "--comment", "ok"])
    assert result.exit_code == 0, result.output
    assert json.loads(seen[0].read()) == {"comment": "ok"}
    assert "approved" in result.output


def test_error_detail_is_shown(gateway):
    _, responses = gateway
    responses[("POST", "/governance/approvals/apr_1/approve")] = httpx.Response(
        403, json={"detail": "self_approval_forbidden"}
    )
    result = runner.invoke(cli.app, ["approvals", "approve", "apr_1"])
    assert result.exit_code == 1
    assert "self_approval_forbidden" in result.output


def test_expired_token_asks_for_login(gateway):
    _, responses = gateway
    responses[("GET", "/governance/approvals")] = httpx.Response(
        401, json={"detail": "invalid_token"}
    )
    result = runner.invoke(cli.app, ["approvals", "list"])
    assert result.exit_code == 1
    assert "agw login" in result.output


def test_audit_verify_fails_on_tampering(gateway):
    _, responses = gateway
    responses[("GET", "/governance/audit/verify")] = httpx.Response(
        200, json={"records": 5, "first_invalid_id": 3}
    )
    result = runner.invoke(cli.app, ["audit", "verify"])
    assert result.exit_code == 1
    assert "3" in result.output
