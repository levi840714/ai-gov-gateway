import json

import httpx

from scripts.env import KEYS_FILE, LITELLM_URL, env

MCP_ACCESS = {
    "object_permission": {
        "mcp_servers": ["probe", "account_ops"],
        "mcp_tool_permissions": {
            "probe": ["whoami", "echo"],
            "account_ops": ["add_to_watchlist", "freeze_account", "file_str"],
        },
    },
}

# kyt_agent v1.5 只需要兩個治理動作；不開放 freeze_account，在 LiteLLM 層也維持最小權限
KYT_V15_ACCESS = {
    "object_permission": {
        "mcp_servers": ["account_ops"],
        "mcp_tool_permissions": {"account_ops": ["add_to_watchlist", "file_str"]},
    },
}

KEYS = {
    "kyt-agent": {"models": ["kyt-main"], "rpm_limit": 60, "max_budget": 5.0},
    "rate-probe": {"models": ["kyt-main"], "rpm_limit": 2, "max_budget": 0.05},
    "kyt-agent-mcp": {
        "object_permission": {
            "mcp_servers": ["probe"],
            "mcp_tool_permissions": {"probe": ["whoami", "echo"]},
        },
    },
    "kyt-agent@1.4": MCP_ACCESS,
    "kyt-agent@1.5": KYT_V15_ACCESS,
    "kyt-agent@9.9": MCP_ACCESS,
}


def create_key(alias: str, body: dict) -> str:
    response = httpx.post(
        f"{LITELLM_URL}/key/generate",
        headers={"Authorization": f"Bearer {env('LITELLM_MASTER_KEY')}"},
        json={"key_alias": alias, **body},
    )
    response.raise_for_status()
    return str(response.json()["key"])


def update_key(key: str, body: dict) -> None:
    response = httpx.post(
        f"{LITELLM_URL}/key/update",
        headers={"Authorization": f"Bearer {env('LITELLM_MASTER_KEY')}"},
        json={"key": key, **body},
    )
    response.raise_for_status()


if __name__ == "__main__":
    KEYS_FILE.parent.mkdir(exist_ok=True)
    stored = json.loads(KEYS_FILE.read_text()) if KEYS_FILE.exists() else {}
    for alias, body in KEYS.items():
        if alias in stored:
            update_key(stored[alias], body)
        else:
            stored[alias] = create_key(alias, body)
    KEYS_FILE.write_text(json.dumps(stored, indent=2))
    print("keys:", ", ".join(sorted(stored)))
