import json

import httpx

from scripts.env import KEYS_FILE, LITELLM_URL, env

# 只開放有 guardrail 的路由：provider pass-through（/gemini、/anthropic、/openai 等）、files、
# threads 不經過 pre_call hook。LiteLLM v1.105.1 依前綴比對，也接受 LiteLLMRoutes 的群組名稱
LLM_ROUTES = {"allowed_routes": ["/v1/chat/completions", "/chat/completions"]}
MCP_ROUTES = {"allowed_routes": ["mcp_inference_routes"]}

MCP_ACCESS = {
    **MCP_ROUTES,
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
    **MCP_ROUTES,
    "object_permission": {
        "mcp_servers": ["account_ops"],
        "mcp_tool_permissions": {"account_ops": ["add_to_watchlist", "file_str"]},
    },
}

KEYS = {
    # 一個 kyt 案件約 13 萬 token，分散在數分鐘內
    "kyt-agent": {
        **LLM_ROUTES,
        "models": ["kyt-main"],
        "rpm_limit": 60,
        "max_budget": 5.0,
        "tpm_limit": 300000,
    },
    "rate-probe": {**LLM_ROUTES, "models": ["kyt-main"], "rpm_limit": 2, "max_budget": 0.05},
    "kyt-agent-mcp": {
        **MCP_ROUTES,
        "object_permission": {
            "mcp_servers": ["probe"],
            "mcp_tool_permissions": {"probe": ["whoami", "echo"]},
        },
    },
    "kyt-agent@1.4": MCP_ACCESS,
    "kyt-agent@1.5": KYT_V15_ACCESS,
    "kyt-agent@9.9": MCP_ACCESS,
    # M4 guardrail 的 demo 與整合測試：kyt 的設定、未登記 agent 的預設、tpm
    "kyt-agent@llm-demo": {**LLM_ROUTES, "models": ["probe-primary", "probe-fallback"]},
    "guard-strict": {**LLM_ROUTES, "models": ["probe-primary", "probe-fallback"]},
    "guard-tpm": {**LLM_ROUTES, "models": ["probe-primary"], "tpm_limit": 1000},
}


def create_key(alias: str, body: dict) -> str:
    response = httpx.post(
        f"{LITELLM_URL}/key/generate",
        headers={"Authorization": f"Bearer {env('LITELLM_MASTER_KEY')}"},
        json={"key_alias": alias, **body},
    )
    response.raise_for_status()
    return str(response.json()["key"])


def update_key(key: str, body: dict) -> bool:
    """回傳 False 表示 LiteLLM 已無此 key（例如 DB 重置），呼叫端需重新建立。"""
    response = httpx.post(
        f"{LITELLM_URL}/key/update",
        headers={"Authorization": f"Bearer {env('LITELLM_MASTER_KEY')}"},
        json={"key": key, **body},
    )
    if response.status_code == 404:
        return False
    response.raise_for_status()
    return True


if __name__ == "__main__":
    KEYS_FILE.parent.mkdir(exist_ok=True)
    stored = json.loads(KEYS_FILE.read_text()) if KEYS_FILE.exists() else {}
    for alias, body in KEYS.items():
        if alias not in stored or not update_key(stored[alias], body):
            stored[alias] = create_key(alias, body)
        KEYS_FILE.write_text(json.dumps(stored, indent=2))
    print("keys:", ", ".join(sorted(stored)))
