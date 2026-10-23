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


def _raise_timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("slow", request=request)


@pytest.mark.parametrize(
    ("handler", "code"),
    [
        (_raise_connect, "policy_unavailable"),
        (_raise_timeout, "policy_unavailable"),
        (lambda request: httpx.Response(500, json={}), "policy_unavailable"),
        (lambda request: httpx.Response(200, json={}), "policy_error"),
        (
            lambda request: httpx.Response(200, json={"result": {"decision": "maybe"}}),
            "policy_error",
        ),
        (lambda request: httpx.Response(200, text="not json"), "policy_error"),
    ],
    ids=["connect", "timeout", "server-error", "no-result", "bad-decision", "not-json"],
)
async def test_failures_map_to_codes(handler, code):
    with pytest.raises(PolicyError) as error:
        await _client(handler).evaluate({})
    assert error.value.code == code


async def test_unserializable_input_is_policy_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("request must not be sent")

    with pytest.raises(PolicyError) as error:
        await _client(handler).evaluate({"x": float("nan")})
    assert error.value.code == "policy_error"


LLM_RESULT = {
    "agent_status": "active",
    "pii": {"entities": ["EMAIL"], "exclude": ["eth_address"]},
    "injection": {"action": "redact", "system_notice": False},
    "high_risk_tools": ["file_str"],
    "policy_version": "2026-10-23.1",
}


async def test_llm_config_posts_agent_and_parses_result():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"result": LLM_RESULT})

    config = await _client(handler).llm_config("kyt-agent")
    assert seen == {
        "path": "/v1/data/governance/llm",
        "body": {"input": {"agent": {"id": "kyt-agent"}}},
    }
    assert config.injection.action == "redact"
    assert config.pii.exclude == ["eth_address"]


@pytest.mark.parametrize(
    "result",
    [
        None,
        {**LLM_RESULT, "pii": {"entities": ["NAME"], "exclude": []}},
        {"agent_status": "active"},
    ],
)
async def test_llm_config_rejects_missing_or_unsupported_result(result):
    client = _client(lambda request: httpx.Response(200, json={"result": result} if result else {}))
    with pytest.raises(PolicyError) as error:
        await client.llm_config("kyt-agent")
    assert error.value.code == "policy_error"


async def test_llm_config_reports_unavailable_opa():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(PolicyError) as error:
        await _client(handler).llm_config("kyt-agent")
    assert error.value.code == "policy_unavailable"
