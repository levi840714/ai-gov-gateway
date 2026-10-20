"""升級 LiteLLM 前必跑：確認 plugin 掛載、hook 欄位與訊息傳遞仍成立。"""

import json
import uuid

import httpx
import pytest

from scripts.agent_sim import call_tool
from scripts.env import KEYS_FILE, LITELLM_URL, env
from scripts.kc import user_token

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def gateway_running():
    try:
        httpx.get(f"{LITELLM_URL}/health/liveliness", timeout=3).raise_for_status()
    except httpx.HTTPError:
        pytest.skip("gateway is not running")


def test_governance_routes_are_mounted_in_litellm():
    assert httpx.get(f"{LITELLM_URL}/governance/approvals").status_code == 401


async def test_hook_fields_reach_policy_and_message_reaches_client():
    # 欄位名稱若改變，server/tool 會是空字串而被判為 unknown_tool，就拿不到 str_always_reviewed
    # account_id 每次不同，以免沿用先前已核准的審批而被兌現
    account_id = f"contract-{uuid.uuid4().hex[:8]}"
    result = await call_tool(
        "kyt-agent@1.4", "alice", "file_str", {"account_id": account_id, "summary": "contract"}
    )
    assert result.status == "pending_approval"
    assert result.body["reasons"] == ["str_always_reviewed"]
    assert result.body["approval_id"].startswith("apr_")


async def test_obo_still_reaches_upstream():
    result = await call_tool("kyt-agent@1.4", "alice", "whoami", {})
    assert result.status == "allowed"
    assert json.loads(result.body["text"])["bearer_claims"]["aud"] == "probe-mcp"


async def test_arguments_reach_policy():
    # 只有 mcp_arguments 傳到 policy，金額才會影響結果；account_id 每次不同以免沿用審批
    arguments = {
        "account_id": f"contract-{uuid.uuid4().hex[:8]}",
        "counterparty": "0x" + "2" * 40,
        "reason": "contract",
    }
    large = await call_tool(
        "kyt-agent@1.4", "alice", "freeze_account", {**arguments, "amount_usd": 25000}
    )
    assert large.status == "pending_approval"
    assert large.body["reasons"] == ["amount_over_threshold"]
    small = await call_tool(
        "kyt-agent@1.4", "alice", "freeze_account", {**arguments, "amount_usd": 5000}
    )
    assert small.status == "allowed"


def _initialize(extra_headers: dict[str, str]) -> httpx.Response:
    headers = {
        "x-litellm-api-key": f"Bearer {json.loads(KEYS_FILE.read_text())['kyt-agent@1.4']}",
        "Authorization": f"Bearer {user_token('alice')}",
        "Accept": "application/json, text/event-stream",
        **extra_headers,
    }
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "contract", "version": "0"},
        },
    }
    return httpx.post(f"{LITELLM_URL}/mcp", json=request, headers=headers, timeout=10)


def test_second_authorization_header_is_rejected():
    # LiteLLM 以 Authorization-* 做 OBO、hook 只驗 Authorization；兩者不一致時稽核與上游身分會分裂
    rejected = _initialize({"Authorization-Obo": f"Bearer {user_token('bob')}"})
    assert rejected.status_code == 400
    assert rejected.json() == {"detail": "ambiguous_authorization_headers"}
    assert _initialize({}).status_code == 200


def test_second_authorization_header_is_rejected_on_llm_routes():
    # LLM 端點也會以 litellm_proxy 執行 MCP tool，檢查不能只限 MCP 路由
    headers = {"Authorization": f"Bearer {env('LITELLM_MASTER_KEY')}"}
    rejected = httpx.get(
        f"{LITELLM_URL}/v1/models", headers=headers | {"Authorization-Obo": "Bearer x"}
    )
    assert rejected.status_code == 400
    assert rejected.json() == {"detail": "ambiguous_authorization_headers"}
    assert httpx.get(f"{LITELLM_URL}/v1/models", headers=headers).status_code == 200
