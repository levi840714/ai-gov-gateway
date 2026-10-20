import json
from dataclasses import dataclass
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from scripts.env import KEYS_FILE, LITELLM_URL
from scripts.kc import user_token


@dataclass(frozen=True)
class ToolResult:
    status: str  # allowed、denied、pending_approval、error
    body: dict[str, Any]


def parse_result(is_error: bool, text: str) -> ToolResult:
    if not is_error:
        return ToolResult("allowed", {"text": text})
    # LiteLLM 會在訊息前加上 "Error: "，治理訊息本身是 JSON
    start = text.find("{")
    try:
        body = json.loads(text[start:]) if start >= 0 else None
    except ValueError:
        body = None
    if isinstance(body, dict) and "governance" in body:
        return ToolResult(str(body["governance"]), body)
    return ToolResult("error", {"text": text})


async def call_tool(key_alias: str, user: str, tool: str, arguments: dict[str, Any]) -> ToolResult:
    headers = {
        "x-litellm-api-key": f"Bearer {json.loads(KEYS_FILE.read_text())[key_alias]}",
        "Authorization": f"Bearer {user_token(user)}",
    }
    async with streamablehttp_client(f"{LITELLM_URL}/mcp", headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            names = [t.name for t in (await session.list_tools()).tools]
            name = next(n for n in names if n.endswith(tool))
            result = await session.call_tool(name, arguments)
    text = result.content[0].text if result.content else ""
    return parse_result(bool(result.isError), text)
