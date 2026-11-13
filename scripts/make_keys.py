import json

import httpx

from scripts.env import KEYS_FILE, LITELLM_URL, env

# 只開放有 guardrail 的路由：provider pass-through（/gemini、/anthropic、/openai 等）、files、
# threads 不經過 pre_call hook。LiteLLM v1.105.1 依前綴比對，也接受 LiteLLMRoutes 的群組名稱
LLM_ROUTES = {"allowed_routes": ["/v1/chat/completions", "/chat/completions"]}
MCP_ROUTES = {"allowed_routes": ["mcp_inference_routes"]}

# 一個 agent 版本一把 key：LLM 與 MCP 共用，guardrail 與 tool 治理都以 key 名稱比對註冊表
AGENT_ROUTES = {"allowed_routes": LLM_ROUTES["allowed_routes"] + MCP_ROUTES["allowed_routes"]}

ALL_TOOLS = {
    "mcp_servers": ["probe", "account_ops"],
    "mcp_tool_permissions": {
        "probe": ["whoami", "echo"],
        "account_ops": ["add_to_watchlist", "freeze_account", "file_str"],
    },
}

# kyt_agent v1.5 只需要兩個治理動作；不開放 freeze_account，在 LiteLLM 層也維持最小權限
KYT_V15_TOOLS = {
    "mcp_servers": ["account_ops"],
    "mcp_tool_permissions": {"account_ops": ["add_to_watchlist", "file_str"]},
}

# 一個 kyt 案件約 13 萬 token，分散在數分鐘內；probe 模型給整合測試與 demo 使用，不花 LLM 費用。
# agent 的總額度依「使用者數 × 個人月額度」設定；個人額度由 gateway 依 OPA 的等級強制
KYT_LLM = {
    "models": ["kyt-main", "probe-primary", "probe-fallback"],
    "rpm_limit": 60,
    "tpm_limit": 300000,
    "max_budget": 30.0,
    "budget_duration": "1mo",  # M5 spike B9：每月 1 日 00:00 UTC 重置
}

KEYS = {
    "kyt-agent-mcp": {
        **MCP_ROUTES,
        "object_permission": {
            "mcp_servers": ["probe"],
            "mcp_tool_permissions": {"probe": ["whoami", "echo"]},
        },
    },
    "kyt-agent@1.4": {**AGENT_ROUTES, **KYT_LLM, "object_permission": ALL_TOOLS},
    "kyt-agent@1.5": {**AGENT_ROUTES, **KYT_LLM, "object_permission": KYT_V15_TOOLS},
    # 未註冊的版本與未登記的 agent：demo 與整合測試確認 LLM 與 tool 呼叫都被拒絕
    "kyt-agent@9.9": {**AGENT_ROUTES, "models": ["probe-primary"], "object_permission": ALL_TOOLS},
    "unknown-agent@1.0": {**LLM_ROUTES, "models": ["probe-primary"]},
}


def create_key(alias: str, body: dict) -> str:
    response = httpx.post(
        f"{LITELLM_URL}/key/generate",
        headers={"Authorization": f"Bearer {env('LITELLM_MASTER_KEY')}"},
        json={"key_alias": alias, **body},
    )
    response.raise_for_status()
    return str(response.json()["key"])


def delete_key(key: str) -> None:
    response = httpx.post(
        f"{LITELLM_URL}/key/delete",
        headers={"Authorization": f"Bearer {env('LITELLM_MASTER_KEY')}"},
        json={"keys": [key]},
    )
    if response.status_code != 404:
        response.raise_for_status()


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
    # 不在 KEYS 裡的 key 一併刪除，停用的 key 不留在 LiteLLM
    for alias in sorted(set(stored) - set(KEYS)):
        delete_key(stored.pop(alias))
        KEYS_FILE.write_text(json.dumps(stored, indent=2))
    for alias, body in KEYS.items():
        if alias not in stored or not update_key(stored[alias], body):
            stored[alias] = create_key(alias, body)
        KEYS_FILE.write_text(json.dumps(stored, indent=2))
    print("keys:", ", ".join(sorted(stored)))
