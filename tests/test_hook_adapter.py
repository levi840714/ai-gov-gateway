import json
from types import SimpleNamespace as NS

import pytest

from ai_gov_gateway.engine.guardrails.pii import PseudonymMap
from ai_gov_gateway.hook_adapter import (
    PendingPseudonyms,
    RestoreError,
    client_body,
    has_ambiguous_authorization,
    restore_response,
    restore_stream,
    tool_call_from_hook,
)

# LiteLLM v1.105.1 call_mcp_tool 的 data 欄位（M1 V7 實際觀察）
M1_DATA = {
    "messages": [],
    "model": "x",
    "mcp_tool_name": "freeze_account",
    "mcp_arguments": {"account_id": "acct-001", "amount_usd": 25000},
    "mcp_server_name": "account_ops",
    "incoming_bearer_token": "eyJ.token.sig",
    "metadata": {},
}


def test_maps_litellm_fields():
    call = tool_call_from_hook(M1_DATA, "kyt-agent@1.4")
    assert call.key_alias == "kyt-agent@1.4"
    assert (call.server, call.tool) == ("account_ops", "freeze_account")
    assert call.arguments == {"account_id": "acct-001", "amount_usd": 25000}
    assert call.user_token == "eyJ.token.sig"


def test_strips_bearer_prefix():
    data = M1_DATA | {"incoming_bearer_token": "Bearer eyJ.token.sig"}
    assert tool_call_from_hook(data, None).user_token == "eyJ.token.sig"


def test_missing_fields_become_empty_and_fail_closed_downstream():
    call = tool_call_from_hook({}, None)
    assert (call.server, call.tool, call.arguments, call.user_token) == ("", "", {}, None)


def test_non_string_token_becomes_none():
    assert tool_call_from_hook(M1_DATA | {"incoming_bearer_token": 123}, None).user_token is None


def test_non_dict_arguments_become_empty():
    assert tool_call_from_hook(M1_DATA | {"mcp_arguments": ["a"]}, None).arguments == {}


@pytest.mark.parametrize(
    "names",
    [
        ["Authorization", "Authorization-Obo"],
        ["authorization-obo"],
        ["AUTHORIZATION_2"],
        ["authorizationx", "x-litellm-api-key"],
        ["Authorization", "authorization"],
    ],
)
def test_authorization_prefixed_headers_are_ambiguous(names):
    assert has_ambiguous_authorization(names)


@pytest.mark.parametrize(
    "names",
    [[], ["Authorization"], ["authorization", "x-litellm-api-key"], ["x-authorization"]],
)
def test_exact_authorization_is_not_ambiguous(names):
    assert not has_ambiguous_authorization(names)


def _pseudonyms() -> PseudonymMap:
    pseudonyms = PseudonymMap()
    pseudonyms.token("EMAIL", 'x"y@example.com')
    return pseudonyms


def test_restore_response_restores_content_and_tool_arguments():
    call = NS(function=NS(arguments=json.dumps({"to": "<EMAIL_1>"})))
    response = NS(choices=[NS(message=NS(content="寄給 <EMAIL_1>", tool_calls=[call]))])
    restore_response(response, _pseudonyms())
    assert response.choices[0].message.content == '寄給 x"y@example.com'
    assert json.loads(call.function.arguments) == {"to": 'x"y@example.com'}


def test_restore_response_keeps_api_keys_out_of_tool_arguments():
    pseudonyms = PseudonymMap()
    pseudonyms.token("API_KEY", "sk-secretsecretsecret")
    call = NS(function=NS(arguments=json.dumps({"key": "<API_KEY_1>"})))
    response = NS(choices=[NS(message=NS(content=None, tool_calls=[call]))])
    restore_response(response, pseudonyms)
    assert json.loads(call.function.arguments) == {"key": "<API_KEY_1>"}


async def _chunks(items):
    for item in items:
        yield item


def _chunk(content=None, calls=None, finish=None):
    delta = NS(content=content, tool_calls=calls)
    return NS(choices=[NS(index=0, delta=delta, finish_reason=finish)])


async def test_restore_stream_joins_split_tokens():
    chunks = [_chunk("寄給 <EM"), _chunk("AIL_1> 了"), _chunk(None, finish="stop")]
    out = [c async for c in restore_stream(_chunks(chunks), _pseudonyms())]
    text = "".join(c.choices[0].delta.content or "" for c in out)
    assert text == '寄給 x"y@example.com 了'


async def test_restore_stream_holds_tool_arguments_until_finish():
    first = NS(index=0, id="c1", function=NS(name="send", arguments='{"to": "<EMA'))
    second = NS(index=0, id=None, function=NS(name=None, arguments='IL_1>"}'))
    chunks = [_chunk(calls=[first]), _chunk(calls=[second]), _chunk(finish="tool_calls")]
    out = [c async for c in restore_stream(_chunks(chunks), _pseudonyms())]
    assert len(out) == 3
    assert json.loads(first.function.arguments) == {"to": 'x"y@example.com'}
    assert second.function.arguments == ""


async def test_restore_stream_flushes_held_text_into_finish_chunk():
    chunks = [_chunk("價格 <"), _chunk(None, finish="stop")]
    out = [c async for c in restore_stream(_chunks(chunks), _pseudonyms())]
    assert "".join(c.choices[0].delta.content or "" for c in out) == "價格 <"


def _choice(index, content=None, calls=None, finish=None):
    return NS(index=index, delta=NS(content=content, tool_calls=calls), finish_reason=finish)


async def test_restore_stream_waits_for_every_choice_with_tool_calls():
    # n=2：choice 0 先結束時，choice 1 的參數還沒收完，不能提早還原
    first = NS(name="send", arguments='{"to": "<EMA')
    second = NS(name=None, arguments='IL_1>"}')
    chunks = [
        NS(choices=[_choice(1, calls=[NS(index=0, id="x", function=first)])]),
        NS(choices=[_choice(0, "done", finish="stop")]),
        NS(choices=[_choice(1, calls=[NS(index=0, id=None, function=second)])]),
        NS(choices=[_choice(1, finish="tool_calls")]),
    ]
    out = [c async for c in restore_stream(_chunks(chunks), _pseudonyms())]
    assert len(out) == 4
    assert json.loads(first.arguments + second.arguments) == {"to": 'x"y@example.com'}


async def test_restore_stream_flushes_held_text_when_stream_ends_without_finish():
    out = [c async for c in restore_stream(_chunks([_chunk("寄給 <EMAIL_1")]), _pseudonyms())]
    assert "".join(c.choices[0].delta.content or "" for c in out) == "寄給 <EMAIL_1"


class _BrokenMap(PseudonymMap):
    def restore(self, text, skip_entities=frozenset()):
        raise TypeError(text)


async def test_restore_stream_wraps_failures_without_content():
    pseudonyms = _BrokenMap()
    pseudonyms.token("EMAIL", "amy@example.com")
    with pytest.raises(RestoreError) as error:
        [c async for c in restore_stream(_chunks([_chunk("amy@example.com")]), pseudonyms)]
    assert str(error.value) == "TypeError"


def test_restore_response_restores_legacy_function_call():
    message = NS(content=None, tool_calls=None, function_call=NS(arguments='{"to": "<EMAIL_1>"}'))
    restore_response(NS(choices=[NS(message=message)]), _pseudonyms())
    assert json.loads(message.function_call.arguments) == {"to": 'x"y@example.com'}


def test_client_body_reads_proxy_server_request():
    body = {"model": "m", "messages": []}
    assert client_body({"proxy_server_request": {"body": body}}) == body
    assert client_body({"proxy_server_request": {"body": None}}) is None
    assert client_body({}) is None


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_pending_pseudonyms_pop_once():
    pending = PendingPseudonyms()
    pending.remember("c1", _pseudonyms())
    assert pending.pop("c1") is not None
    assert pending.pop("c1") is None
    assert pending.pop(None) is None


def test_pending_pseudonyms_drop_expired_and_excess_entries(caplog):
    clock = _Clock()
    pending = PendingPseudonyms(max_entries=2, ttl_seconds=600, clock=clock)
    pending.remember("old", _pseudonyms())
    clock.now = 601
    pending.remember("a", _pseudonyms())
    pending.remember("b", _pseudonyms())
    pending.remember("c", _pseudonyms())
    assert (pending.pop("old"), pending.pop("a")) == (None, None)
    assert len(pending) == 2
    assert [r.getMessage() for r in caplog.records] == [
        "dropped pending pseudonym maps: expired=1 evicted=0",
        "dropped pending pseudonym maps: expired=0 evicted=1",
    ]
    assert "example.com" not in caplog.text
