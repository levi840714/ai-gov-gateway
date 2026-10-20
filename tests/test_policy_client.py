import json

import httpx
import pytest

from ai_gov_gateway.engine.models import Decision
from ai_gov_gateway.engine.policy import OpaPolicyClient, PolicyError


def _client(handler) -> OpaPolicyClient:
    return OpaPolicyClient("http://opa:8181", 2.0, transport=httpx.MockTransport(handler))


async def test_posts_input_and_parses_decision():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        result = {
            "decision": "require_approval",
            "reasons": ["amount_over_threshold"],
            "approver_role": "compliance_supervisor",
            "policy_version": "2026-10-20.1",
        }
        return httpx.Response(200, json={"result": result})

    decision = await _client(handler).evaluate({"tool": {"name": "freeze_account"}})
    assert seen == {
        "path": "/v1/data/governance/decision",
        "body": {"input": {"tool": {"name": "freeze_account"}}},
    }
    assert decision.decision is Decision.REQUIRE_APPROVAL
    assert decision.policy_version == "2026-10-20.1"


def _raise_connect(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


@pytest.mark.parametrize(
    ("handler", "code"),
    [
        (_raise_connect, "policy_unavailable"),
        (lambda request: httpx.Response(500, json={}), "policy_unavailable"),
        (lambda request: httpx.Response(200, json={}), "policy_error"),
        (
            lambda request: httpx.Response(200, json={"result": {"decision": "maybe"}}),
            "policy_error",
        ),
        (lambda request: httpx.Response(200, text="not json"), "policy_error"),
    ],
    ids=["connect", "server-error", "no-result", "bad-decision", "not-json"],
)
async def test_failures_map_to_codes(handler, code):
    with pytest.raises(PolicyError) as error:
        await _client(handler).evaluate({})
    assert error.value.code == code
