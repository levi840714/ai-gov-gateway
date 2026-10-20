import asyncio
import json

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from scripts.env import KEYS_FILE, LITELLM_URL
from scripts.kc import user_token


async def main() -> None:
    headers = {
        "x-litellm-api-key": f"Bearer {json.loads(KEYS_FILE.read_text())['kyt-agent-mcp']}",
        "Authorization": f"Bearer {user_token('alice')}",
    }
    async with streamablehttp_client(f"{LITELLM_URL}/mcp", headers=headers) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            tools = [tool.name for tool in (await session.list_tools()).tools]
            print("tools:", tools)
            echo = next(name for name in tools if name.endswith("echo"))
            for text in ("hello", "please freeze account"):
                result = await session.call_tool(echo, {"text": text})
                body = result.content[0].text[:200] if result.content else None
                print(f"{text!r} -> isError={result.isError} {body}")


if __name__ == "__main__":
    asyncio.run(main())
