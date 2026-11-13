import json

import pytest

from ai_gov_gateway.engine.guardrails.fields import check_request, end_user_field, text_length
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


IMAGE = "https://img.example.com/a.png"


@pytest.mark.parametrize("detail", [None, "low", "high", "auto"])
def test_image_parts_with_url_and_detail_pass(detail):
    image = {"url": IMAGE} if detail is None else {"url": IMAGE, "detail": detail}
    message = {"role": "user", "content": [{"type": "image_url", "image_url": image}]}
    check_request({"model": "m", "messages": [message]}, {"messages": [message]})


@pytest.mark.parametrize(
    "image, field",
    [
        (IMAGE, "messages.content.image_url.image_url"),
        ({"detail": "low"}, "messages.content.image_url.image_url"),
        ({"url": IMAGE, "detail": PII}, "messages.content.image_url.image_url.detail"),
        ({"url": IMAGE, "alt": PII}, "messages.content.image_url.image_url.alt"),
    ],
)
def test_image_parts_reject_other_shapes(image, field):
    message = {"role": "user", "content": [{"type": "image_url", "image_url": image}]}
    assert _field({"model": "m", "messages": [message]}) == field


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"user": "alice"}, "user"),
        ({"metadata": {"user_id": "x"}}, "metadata.user_id"),
        ({"metadata": {"user": "x"}}, "metadata.user"),
        ({"metadata": '{"user_id": "x"}'}, "metadata.user_id"),  # LiteLLM 也會解析字串形式
        ({"user": ""}, "user"),  # 空字串 LiteLLM 雖然不採用，仍視為自帶身份
    ],
)
def test_end_user_field_detected(body, field):
    assert end_user_field(body) == field


def test_no_end_user_field():
    assert end_user_field({"model": "m", "messages": [], "metadata": {"tag": "x"}}) is None
    assert end_user_field(None) is None
    assert end_user_field({"metadata": "not json"}) is None


def test_text_length_counts_content_and_tool_arguments():
    messages = [
        {"role": "user", "content": "你好"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "t", "type": "function", "function": {"name": "f", "arguments": '{"a":1}'}}
            ],
        },
        {"role": "user", "content": [{"type": "text", "text": "abc"}]},
    ]
    assert text_length(messages) == 2 + 7 + 3
