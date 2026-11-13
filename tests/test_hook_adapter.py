import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace as NS

import pytest

from ai_gov_gateway.engine.fingerprint import sha256_hex
from ai_gov_gateway.engine.guardrails.pii import PseudonymMap
from ai_gov_gateway.engine.ledger import Settlement
from ai_gov_gateway.engine.llm_gate import LlmRequest
from ai_gov_gateway.hook_adapter import (
    AdmittedCalls,
    DuplicateCallId,
    PendingPseudonyms,
    PseudonymsEvicted,
    RestoreError,
    client_body,
    end_user_header,
    has_ambiguous_authorization,
    llm_request_from_hook,
    owner_of,
    restore_response,
    restore_stream,
    settlement_from_log,
    tool_call_from_hook,
    user_token_from_hook,
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


def _stored(value: str = "amy@example.com") -> PseudonymMap:
    pseudonyms = PseudonymMap()
    pseudonyms.token("EMAIL", value)
    return pseudonyms


def test_owner_is_a_hash_of_the_litellm_token():
    owner = owner_of(NS(token="hashed-key", api_key="sk-raw"))
    assert owner is not None and len(owner) == 64 and "hashed-key" not in owner
    assert owner_of(NS(token=None, api_key="sk-raw")) not in (None, owner)
    assert owner_of(NS()) is None


def test_pending_pseudonyms_pop_once():
    pending = PendingPseudonyms()
    pending.remember("o1", "c1", _stored())
    assert pending.pop("o1", "c1") is not None
    assert pending.pop("o1", "c1") is None
    assert pending.pop("o1", None) is None
    assert pending.pop(None, "c1") is None


def test_pending_pseudonyms_are_scoped_to_the_caller():
    pending = PendingPseudonyms()
    pending.remember("o1", "c1", _stored())
    pending.remember("o2", "c1", _stored("bob@example.com"))
    assert pending.pop("o2", "c1").restore("<EMAIL_1>") == "bob@example.com"
    assert pending.pop("o1", "c1").restore("<EMAIL_1>") == "amy@example.com"


def test_duplicate_call_id_is_rejected_instead_of_overwritten():
    pending = PendingPseudonyms()
    pending.remember("o1", "c1", _stored())
    with pytest.raises(DuplicateCallId):
        pending.remember("o1", "c1", _stored("bob@example.com"))
    assert pending.pop("o1", "c1").restore("<EMAIL_1>") == "amy@example.com"


class _SpyMap(PseudonymMap):
    discarded = False

    def discard_cache(self) -> None:
        self.discarded = True
        super().discard_cache()


def test_compiled_pattern_is_discarded_before_storing():
    pseudonyms = _SpyMap()
    pseudonyms.token("EMAIL", "amy@example.com")
    PendingPseudonyms().remember("o1", "c1", pseudonyms)
    assert pseudonyms.discarded


def test_pending_pseudonyms_drop_expired_and_excess_entries(caplog):
    clock = _Clock()
    pending = PendingPseudonyms(max_entries=2, ttl_seconds=600, clock=clock)
    pending.remember("o", "old", _stored())
    clock.now = 601
    pending.remember("o", "a", _stored())
    pending.remember("o", "b", _stored())
    pending.remember("o", "c", _stored())
    assert len(pending) == 2
    assert [r.getMessage() for r in caplog.records] == [
        "dropped pending pseudonym maps: expired=1 evicted=0",
        "dropped pending pseudonym maps: expired=0 evicted=1",
    ]
    assert "example.com" not in caplog.text


def test_pending_pseudonyms_are_bounded_by_total_value_chars():
    pending = PendingPseudonyms(max_value_chars=40)
    pending.remember("o", "a", _stored("a" * 20 + "@example.com"))
    pending.remember("o", "b", _stored("b" * 20 + "@example.com"))
    assert len(pending) == 1
    assert pending.pop("o", "b") is not None
    pending.remember("o", "c", _stored("c" * 20 + "@example.com"))
    assert len(pending) == 1


def test_evicted_pseudonyms_fail_instead_of_returning_tokens():
    pending = PendingPseudonyms(max_entries=1)
    pending.remember("o", "a", _stored())
    pending.remember("o", "b", _stored())
    with pytest.raises(PseudonymsEvicted):
        pending.pop("o", "a")
    assert pending.pop("o", "a") is None


def test_evicted_call_id_cannot_be_reused_before_it_is_reported():
    pending = PendingPseudonyms(max_entries=1)
    pending.remember("o", "a", _stored())
    pending.remember("o", "b", _stored())
    with pytest.raises(DuplicateCallId):
        pending.remember("o", "a", _stored())


def test_discard_ignores_evicted_entries():
    pending = PendingPseudonyms(max_entries=1)
    pending.remember("o", "a", _stored())
    pending.remember("o", "b", _stored())
    pending.discard("o", "a")
    pending.discard("o", "b")
    assert len(pending) == 0
    assert pending.pop("o", "a") is None


def test_evicted_keys_are_bounded():
    pending = PendingPseudonyms(max_entries=1, max_evicted=1)
    for call_id in ("a", "b", "c"):
        pending.remember("o", call_id, _stored())
    assert pending.pop("o", "a") is None
    with pytest.raises(PseudonymsEvicted):
        pending.pop("o", "b")


def _data(**headers: str) -> dict:
    return {"secret_fields": {"raw_headers": headers}}


def test_user_token_from_raw_authorization():
    assert user_token_from_hook(_data(authorization="Bearer eyJ.a.b")) == "eyJ.a.b"


def test_user_token_missing_when_no_secret_fields():
    assert user_token_from_hook({}) is None
    assert user_token_from_hook(_data()) is None


def test_user_token_header_name_is_case_insensitive():
    assert user_token_from_hook(_data(Authorization="Bearer t")) == "t"


@pytest.mark.parametrize("name", ["x-litellm-customer-id", "X-LiteLLM-End-User-Id"])
def test_end_user_header_detected(name):
    assert end_user_header(_data(**{name: "someone"})) == name.lower()


def test_no_end_user_header():
    assert end_user_header(_data(authorization="Bearer t")) is None


def _chat_data(**extra) -> dict:
    return {
        **_data(authorization="Bearer eyJ.a.b"),
        "model": "probe-primary",
        "litellm_call_id": "c1",
        "messages": [{"role": "user", "content": "hi"}],
        "proxy_server_request": {"body": {"model": "probe-primary"}},
        **extra,
    }


def test_llm_request_carries_n():
    request = llm_request_from_hook(_chat_data(n=2), NS(key_alias=None, token=None))
    assert request.n == 2


def test_llm_request_from_hook():
    key = NS(key_alias="kyt-agent@1.6", token="hashed-key")
    request = llm_request_from_hook(_chat_data(max_tokens=17), key)
    assert request == LlmRequest(
        key_alias="kyt-agent@1.6",
        owner=owner_of(key),
        user_token="eyJ.a.b",
        end_user_override=None,
        model="probe-primary",
        call_id="c1",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=17,
        max_completion_tokens=None,
        n=None,
    )
    assert "eyJ" not in repr(request)


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ({"proxy_server_request": {"body": {"user": "x"}}}, "user"),
        ({"proxy_server_request": {"body": {"metadata": {"user_id": "x"}}}}, "metadata.user_id"),
        (_data(**{"x-litellm-customer-id": "x"}), "x-litellm-customer-id"),
    ],
)
def test_llm_request_reports_end_user_override(extra, expected):
    request = llm_request_from_hook(_chat_data(**extra), NS(key_alias=None, token=None))
    assert request.end_user_override == expected


NOW = datetime(2026, 11, 13, 9, tzinfo=UTC)


def _success_kwargs(**log) -> dict:
    standard = {
        "id": "not-the-call-id",
        "response_cost": 0.0123,
        "prompt_tokens": 400,
        "completion_tokens": 56,
        "metadata": {"user_api_key_hash": "hashed-key"},
        **log,
    }
    return {"litellm_call_id": "c1", "standard_logging_object": standard}


def test_settlement_from_success_kwargs():
    assert settlement_from_log(_success_kwargs(), at=NOW) == Settlement(
        owner=sha256_hex("hashed-key"),
        call_id="c1",
        cost_usd=Decimal("0.0123"),
        prompt_tokens=400,
        completion_tokens=56,
        at=NOW,
        settled_by="callback",
    )


def test_settlement_owner_matches_owner_of():
    settlement = settlement_from_log(_success_kwargs(), at=NOW)
    assert settlement.owner == owner_of(NS(token="hashed-key"))


def test_settlement_cost_drops_float_error():
    settlement = settlement_from_log(_success_kwargs(response_cost=0.00012200000000000001), at=NOW)
    assert settlement.cost_usd == Decimal("0.000122")
    assert str(settlement.cost_usd) == "0.000122"


@pytest.mark.parametrize(
    "broken",
    [
        {},
        {"litellm_call_id": "c1"},
        {"litellm_call_id": "c1", "standard_logging_object": {"response_cost": "x"}},
        {**_success_kwargs(), "litellm_call_id": ""},
        _success_kwargs(response_cost=None),
        _success_kwargs(response_cost=True),
        _success_kwargs(response_cost=float("nan")),
        _success_kwargs(response_cost=-0.1),
        _success_kwargs(prompt_tokens=None),
        _success_kwargs(completion_tokens=1.5),
        _success_kwargs(metadata={}),
    ],
)
def test_settlement_missing_fields_returns_none(broken):
    assert settlement_from_log(broken, at=NOW) is None


def test_admitted_call_is_released_once():
    admitted = AdmittedCalls()
    admitted.add("o", "c1")
    assert admitted.pop_for_release("o", "c1") is True
    assert admitted.pop_for_release("o", "c1") is False


def test_rejected_duplicate_does_not_release_the_original():
    admitted = AdmittedCalls()
    admitted.add("o", "c1")
    admitted.rejected("o", "c1")
    assert admitted.pop_for_release("o", "c1") is False  # 被拒絕的重複請求
    assert admitted.pop_for_release("o", "c1") is True  # 原本的請求失敗


def test_success_forgets_the_call():
    admitted = AdmittedCalls()
    admitted.add("o", "c1")
    admitted.rejected("o", "c1")
    admitted.forget("o", "c1")
    assert admitted.pop_for_release("o", "c1") is False
    assert len(admitted) == 0


def test_rejection_of_unknown_call_is_ignored():
    admitted = AdmittedCalls()
    admitted.rejected("o", "c1")
    admitted.add("o", "c1")
    assert admitted.pop_for_release("o", "c1") is True


def test_admitted_calls_expire():
    clock = _Clock()
    admitted = AdmittedCalls(ttl_seconds=600, clock=clock)
    admitted.add("o", "c1")
    clock.now += 601
    assert admitted.pop_for_release("o", "c1") is False
    assert len(admitted) == 0


def test_admitted_calls_are_bounded():
    admitted = AdmittedCalls(max_entries=2)
    for call_id in ("c1", "c2", "c3"):
        admitted.add("o", call_id)
    assert len(admitted) == 2
    assert admitted.pop_for_release("o", "c1") is False
    assert admitted.pop_for_release("o", "c3") is True


@pytest.mark.parametrize(("owner", "call_id"), [(None, "c1"), ("o", None), ("o", ""), ("o", 5)])
def test_admitted_calls_ignore_missing_keys(owner, call_id):
    admitted = AdmittedCalls()
    admitted.add(owner, call_id)
    admitted.rejected(owner, call_id)
    assert admitted.pop_for_release(owner, call_id) is False
    assert len(admitted) == 0


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ({"user": "x"}, "user"),
        ({"metadata": {"user_id": "x"}}, "metadata.user_id"),
        ({"metadata": {"user": "x"}}, "metadata.user"),
    ],
)
def test_end_user_override_falls_back_to_data_without_client_body(extra, expected):
    data = {**_chat_data(**extra), "proxy_server_request": {"body": None}}
    request = llm_request_from_hook(data, NS(key_alias=None, token=None))
    assert request.end_user_override == expected


def test_litellm_own_metadata_is_not_an_end_user_override():
    metadata = {"user_api_key_user_id": "u", "user_api_key_hash": "h", "user_api_key_alias": "a"}
    data = {**_chat_data(metadata=metadata), "proxy_server_request": {}}
    request = llm_request_from_hook(data, NS(key_alias=None, token=None))
    assert request.end_user_override is None
