import argparse
import asyncio
import json

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from scripts.env import KEYS_FILE, LITELLM_URL
from scripts.kc import user_token


async def run(key_alias: str, user: str | None) -> None:
    headers = {"x-litellm-api-key": f"Bearer {json.loads(KEYS_FILE.read_text())[key_alias]}"}
    if user:
        headers["Authorization"] = f"Bearer {user_token(user)}"
    async with streamablehttp_client(f"{LITELLM_URL}/mcp", headers=headers) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            tools = [tool.name for tool in (await session.list_tools()).tools]
            print("tools:", tools)
            whoami = next((name for name in tools if name.endswith("whoami")), None)
            if whoami is None:
                return
            result = await session.call_tool(whoami, {})
            print("isError:", result.isError)
            print(result.content[0].text if result.content else "<empty>")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--key", default="kyt-agent-mcp")
    parser.add_argument("--user", default="alice", help="use '' to send no user token")
    args = parser.parse_args()
    asyncio.run(run(args.key, args.user or None))
