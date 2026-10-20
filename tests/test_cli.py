import json

import httpx
import pytest
from typer.testing import CliRunner

from ai_gov_gateway import cli
from ai_gov_gateway.cli_auth import load_token, save_token

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


def test_list_all_sends_no_status_param(gateway):
    seen, responses = gateway
    responses[("GET", "/governance/approvals")] = httpx.Response(200, json=[])
    result = runner.invoke(cli.app, ["approvals", "list", "--all"])
    assert result.exit_code == 0, result.output
    assert "status" not in seen[0].url.params


def test_invalid_status_is_rejected_locally(gateway):
    seen, _ = gateway
    result = runner.invoke(cli.app, ["approvals", "list", "--status", "foo"])
    assert result.exit_code != 0
    assert seen == []


def test_network_error_is_reported_without_traceback(monkeypatch, gateway):
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("secret detail", request=request)

    monkeypatch.setattr(
        cli, "make_client", lambda: httpx.Client(transport=httpx.MockTransport(refuse))
    )
    result = runner.invoke(cli.app, ["approvals", "list"])
    assert result.exit_code == 1
    assert "無法連線" in result.output and "ConnectError" in result.output
    assert "secret detail" not in result.output
    assert "Traceback" not in result.output


def test_error_with_list_detail_is_printed_as_json(gateway):
    _, responses = gateway
    detail = [{"loc": ["body", "comment"], "msg": "too long"}]
    responses[("POST", "/governance/approvals/apr_1/approve")] = httpx.Response(
        422, json={"detail": detail}
    )
    result = runner.invoke(cli.app, ["approvals", "approve", "apr_1"])
    assert result.exit_code == 1
    assert "too long" in result.output
    assert "Traceback" not in result.output


def test_error_with_non_object_body(gateway):
    _, responses = gateway
    responses[("GET", "/governance/approvals")] = httpx.Response(500, json=["boom"])
    result = runner.invoke(cli.app, ["approvals", "list"])
    assert result.exit_code == 1
    assert "500" in result.output
    assert "Traceback" not in result.output


def test_html_success_body_is_rejected(gateway):
    _, responses = gateway
    responses[("GET", "/governance/approvals")] = httpx.Response(200, text="<html>proxy</html>")
    result = runner.invoke(cli.app, ["approvals", "list"])
    assert result.exit_code == 1
    assert "閘道回應格式不正確" in result.output
    assert "Traceback" not in result.output


def test_login_saves_token_without_echoing_it(monkeypatch, tmp_path):
    token_path = tmp_path / "token.json"
    monkeypatch.setenv("AGW_TOKEN_PATH", str(token_path))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/device"):
            return httpx.Response(
                200,
                json={
                    "device_code": "dev",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": "http://kc/device",
                    "expires_in": 600,
                },
            )
        return httpx.Response(200, json={"access_token": "fresh-token"})

    monkeypatch.setattr(
        cli, "make_client", lambda: httpx.Client(transport=httpx.MockTransport(handler))
    )
    result = runner.invoke(cli.app, ["login"])
    assert result.exit_code == 0, result.output
    assert "ABCD-EFGH" in result.output
    assert "fresh-token" not in result.output
    assert load_token(token_path) == "fresh-token"


def test_login_network_error(monkeypatch, tmp_path):
    monkeypatch.setenv("AGW_TOKEN_PATH", str(tmp_path / "token.json"))

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("x", request=request)

    monkeypatch.setattr(
        cli, "make_client", lambda: httpx.Client(transport=httpx.MockTransport(refuse))
    )
    result = runner.invoke(cli.app, ["login"])
    assert result.exit_code == 1
    assert "無法連線" in result.output
