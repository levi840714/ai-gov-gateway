import hashlib
import json
from typing import Any

from ai_gov_gateway.engine.models import AgentRef


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def call_fingerprint(
    agent: AgentRef, user_sub: str, server: str, tool: str, arguments: dict[str, Any]
) -> str:
    """核准綁定的對象：同一個 agent 版本、委託人、tool 與參數。"""
    payload = {
        "agent_id": agent.id,
        "agent_version": agent.version,
        "user_sub": user_sub,
        "server": server,
        "tool": tool,
        "arguments": arguments,
    }
    return sha256_hex(canonical_json(payload))


def parse_agent_key(key_alias: str | None) -> AgentRef | None:
    """virtual key 依 `<agent>@<版本>` 命名；不符合格式視為未註冊。"""
    if not key_alias or key_alias.count("@") != 1:
        return None
    agent_id, version = key_alias.split("@")
    if not agent_id or not version:
        return None
    return AgentRef(id=agent_id, version=version, key_alias=key_alias)
