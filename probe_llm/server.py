"""OpenAI 相容的測試 provider：記錄收到的請求，依 system 訊息中的 probe: 指令回應。

指令一行一個：probe:reply=echo|<文字>、probe:tool=<名稱>、probe:fail=<模型>、
probe:usage=<prompt token 數>、probe:split=<streaming 切分位置>。
"""

import json
import os
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

RECORDED: deque[dict[str, Any]] = deque(maxlen=100)
# 這些模型一律回 503：真實 agent 的 prompt 不含 probe: 指令，強制 fallback 時用環境變數設定
FAIL_MODELS = {m for m in os.environ.get("PROBE_FAIL_MODELS", "").split(",") if m}


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return ""


def directives(body: dict[str, Any]) -> dict[str, str]:
    found: dict[str, str] = {}
    for message in body.get("messages", []):
        if message.get("role") != "system":
            continue
        for line in _text(message.get("content")).splitlines():
            line = line.strip()
            if line.startswith("probe:") and "=" in line:
                key, value = line.removeprefix("probe:").split("=", 1)
                found[key.strip()] = value.strip()
    return found


def _last_user_text(body: dict[str, Any]) -> str:
    users = [m for m in body.get("messages", []) if m.get("role") == "user"]
    return _text(users[-1].get("content")) if users else ""


def completion(body: dict[str, Any], found: dict[str, str]) -> dict[str, Any]:
    reply = found.get("reply", "ok")
    message: dict[str, Any] = {
        "role": "assistant",
        "content": _last_user_text(body) if reply == "echo" else reply,
    }
    finish = "stop"
    if tool := found.get("tool"):
        arguments = json.dumps({"text": _last_user_text(body)}, ensure_ascii=False)
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_probe_1",
                    "type": "function",
                    "function": {"name": tool, "arguments": arguments},
                }
            ],
        }
        finish = "tool_calls"
    prompt_tokens = int(found.get("usage", "0")) or max(
        1, len(json.dumps(body.get("messages", []), ensure_ascii=False)) // 4
    )
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.get("model", "probe"),
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": 10,
            "total_tokens": prompt_tokens + 10,
        },
    }


def stream_chunks(result: dict[str, Any], split: int) -> list[dict[str, Any]]:
    base = {k: result[k] for k in ("id", "created", "model")} | {"object": "chat.completion.chunk"}

    def chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
        return {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

    choice = result["choices"][0]
    message = choice["message"]
    chunks = [chunk({"role": "assistant"})]
    if message.get("tool_calls"):
        call = message["tool_calls"][0]
        arguments = call["function"]["arguments"]
        first = {"name": call["function"]["name"], "arguments": arguments[:split]}
        chunks.append(
            chunk(
                {
                    "tool_calls": [
                        {"index": 0, "id": call["id"], "type": "function", "function": first}
                    ]
                }
            )
        )
        chunks.append(
            chunk({"tool_calls": [{"index": 0, "function": {"arguments": arguments[split:]}}]})
        )
    else:
        text = message["content"] or ""
        chunks += [chunk({"content": part}) for part in (text[:split], text[split:]) if part]
    final = chunk({}, choice["finish_reason"])
    final["usage"] = result["usage"]
    return [*chunks, final]


async def chat(request: Request) -> Response:
    body = await request.json()
    RECORDED.append(body)
    found = directives(body)
    if body.get("model") in FAIL_MODELS or found.get("fail") == body.get("model"):
        error = {"error": {"message": "probe unavailable", "type": "server_error"}}
        return JSONResponse(error, status_code=503)
    result = completion(body, found)
    if not body.get("stream"):
        return JSONResponse(result)
    split = int(found.get("split", "1"))

    async def events() -> AsyncIterator[str]:
        for item in stream_chunks(result, split):
            yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


async def recorded(request: Request) -> Response:
    if request.method == "DELETE":
        RECORDED.clear()
        return Response(status_code=204)
    return JSONResponse(list(RECORDED))


async def health(request: Request) -> Response:
    return JSONResponse({"ok": True})


app = Starlette(
    routes=[
        Route("/v1/chat/completions", chat, methods=["POST"]),
        Route("/requests", recorded, methods=["GET", "DELETE"]),
        Route("/health", health),
    ]
)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8300, log_level="warning")
