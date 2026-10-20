"""升級 LiteLLM 前必跑：確認 plugin 掛載、hook 欄位與訊息傳遞仍成立。"""

import json
import uuid

import httpx
import pytest

from scripts.agent_sim import call_tool
from scripts.env import LITELLM_URL

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
    result = await call_tool(
        "kyt-agent@1.4", "alice", "file_str", {"account_id": "contract", "summary": "contract"}
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
