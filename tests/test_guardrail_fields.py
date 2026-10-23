import json

import pytest

from ai_gov_gateway.engine.guardrails.fields import check_request
from ai_gov_gateway.engine.guardrails.models import GuardrailError

PII = "A123456789"
MESSAGES = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]
# kyt_agent 以 langchain-openai 的 bind_tools 與 with_structured_output 實際送出的欄位
KYT_BODY = {
    "model": "kyt-main",
    "stream": False,
    "metadata": {},
    "tools": [{"type": "function", "function": {"name": "f", "parameters": {}}}],
    "response_format": {"type": "json_schema", "json_schema": {"name": "x", "schema": {}}},
    "messages": [
        *MESSAGES,
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "r"},
    ],
}


def _field(body, data=None) -> str:
    data = data if data is not None else body
    with pytest.raises(GuardrailError) as error:
        check_request(body, data)
    assert (error.value.status, error.value.code) == (400, "unsupported_field")
    assert PII not in json.dumps(error.value.body())
    return error.value.body()["field"]


def test_known_client_fields_pass():
    check_request(KYT_BODY, KYT_BODY)


@pytest.mark.parametrize(
    "name", ["prompt", "system", "input", "extra_body", "prediction", "functions"]
)
def test_unknown_top_level_fields_are_rejected(name):
    body = {"model": "m", "messages": MESSAGES, name: f"id {PII}"}
    assert _field(body) == name


def test_litellm_internal_keys_in_data_are_not_checked_when_body_is_known():
    body = {"model": "m", "messages": MESSAGES, "metadata": {}}
    data = {**body, "litellm_call_id": "x", "proxy_server_request": {}, "secret_fields": {}}
    check_request(body, data)


def test_without_body_known_forwarded_fields_are_rejected():
    data = {"model": "m", "messages": MESSAGES, "litellm_call_id": "x", "prompt": PII}
    assert _field(None, data) == "prompt"
    check_request(None, {"model": "m", "messages": MESSAGES, "litellm_call_id": "x"})


@pytest.mark.parametrize(
    "message, field",
    [
        ({"role": "assistant", "function_call": {"arguments": PII}}, "messages.function_call"),
        (
            {"role": "assistant", "content": "ok", "reasoning_content": PII},
            "messages.reasoning_content",
        ),
        ({"role": "user", "content": {"type": "text", "text": PII}}, "messages.content"),
        (
            {"role": "user", "content": [{"type": "input_text", "content": PII}]},
            "messages.content.input_text",
        ),
        (
            {"role": "user", "content": [{"type": "text", "text": "a", "extra": PII}]},
            "messages.content.text.extra",
        ),
        ({"role": "user", "content": ["plain"]}, "messages.content.?"),
    ],
)
def test_unknown_message_fields_are_rejected(message, field):
    assert _field({"model": "m", "messages": [message]}) == field


def test_unknown_tool_call_fields_are_rejected():
    call = {"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}", "x": PII}}
    body = {"model": "m", "messages": [{"role": "assistant", "tool_calls": [call]}]}
    assert _field(body) == "messages.tool_calls.function.x"


def test_empty_values_carry_nothing_and_pass():
    message = {"role": "assistant", "content": "ok", "refusal": None, "annotations": []}
    check_request({"model": "m", "messages": [message], "functions": []}, {"messages": [message]})


def test_field_names_that_could_hold_content_are_not_echoed():
    assert _field({"model": "m", "messages": MESSAGES, PII: 1}) == "?"
