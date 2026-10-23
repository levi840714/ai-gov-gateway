import json

import httpx
import pytest

from probe_llm.server import FAIL_MODELS, RECORDED, app


@pytest.fixture(autouse=True)
def clear_recorded():
    RECORDED.clear()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://probe")


def _body(*system: str, user: str = "hello", stream: bool = False, model: str = "probe-primary"):
    messages = [{"role": "system", "content": line} for line in system]
    messages.append({"role": "user", "content": user})
    return {"model": model, "messages": messages, "stream": stream}


async def test_echo_reply_returns_last_user_message_and_records_request():
    async with _client() as client:
        response = await client.post(
            "/v1/chat/completions", json=_body("probe:reply=echo", user="hi <EMAIL_1>")
        )
        recorded = (await client.get("/requests")).json()
    choice = response.json()["choices"][0]
    assert choice["message"]["content"] == "hi <EMAIL_1>"
    assert choice["finish_reason"] == "stop"
    assert recorded[-1]["messages"][-1]["content"] == "hi <EMAIL_1>"


async def test_tool_directive_returns_tool_call_with_user_text_as_argument():
    async with _client() as client:
        response = await client.post(
            "/v1/chat/completions", json=_body("probe:tool=send_mail", user='to "<EMAIL_1>"')
        )
    call = response.json()["choices"][0]["message"]["tool_calls"][0]
    assert call["function"]["name"] == "send_mail"
    assert json.loads(call["function"]["arguments"]) == {"text": 'to "<EMAIL_1>"'}


async def test_fail_directive_only_affects_named_model():
    async with _client() as client:
        failed = await client.post("/v1/chat/completions", json=_body("probe:fail=probe-primary"))
        other = await client.post(
            "/v1/chat/completions", json=_body("probe:fail=probe-primary", model="probe-fallback")
        )
    assert failed.status_code == 503
    assert other.status_code == 200
    assert other.json()["model"] == "probe-fallback"


async def test_fail_models_env_forces_503():
    FAIL_MODELS.add("kyt-main")
    try:
        async with _client() as client:
            response = await client.post("/v1/chat/completions", json=_body(model="kyt-main"))
    finally:
        FAIL_MODELS.discard("kyt-main")
    assert response.status_code == 503


async def test_usage_directive_sets_prompt_tokens():
    async with _client() as client:
        response = await client.post("/v1/chat/completions", json=_body("probe:usage=5000"))
    assert response.json()["usage"]["prompt_tokens"] == 5000


async def test_stream_splits_content_at_requested_position():
    async with _client() as client:
        response = await client.post(
            "/v1/chat/completions",
            json=_body("probe:reply=echo", "probe:split=4", user="abc <TW_ID_1> def", stream=True),
        )
    events = [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]
    assert events[-1] == "[DONE]"
    chunks = [json.loads(event) for event in events[:-1]]
    parts = [
        c["choices"][0]["delta"].get("content")
        for c in chunks
        if c["choices"][0]["delta"].get("content")
    ]
    assert parts == ["abc ", "<TW_ID_1> def"]
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


async def test_delete_clears_recorded_requests():
    async with _client() as client:
        await client.post("/v1/chat/completions", json=_body())
        await client.delete("/requests")
        assert (await client.get("/requests")).json() == []
