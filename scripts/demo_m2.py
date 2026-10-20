"""依序執行 spec §9 的 13 個端到端情境；任一失敗時結束碼為 1。"""

import asyncio
import json
import subprocess
import sys
import time
import uuid
from typing import Any

import httpx

from scripts.agent_sim import ToolResult, call_tool
from scripts.env import LITELLM_URL, ROOT
from scripts.kc import user_token

AGENTS_FILE = ROOT / "policies" / "agents" / "data.json"
CLEAN = "0x1111111111111111111111111111111111111111"
SANCTIONED = "0x8589427373D6D84E98730D7795D8F6F8731FDA16"
# 每次執行用不同帳號，避免沿用上一次留下的審批
RUN = uuid.uuid4().hex[:6]
results: list[bool] = []


def check(name: str, ok: bool, detail: str) -> None:
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


def compose(*args: str) -> str:
    done = subprocess.run(
        ["docker", "compose", *args], cwd=ROOT, check=True, capture_output=True, text=True
    )
    return done.stdout


def restart_opa() -> None:
    compose("restart", "opa")
    time.sleep(3)


def psql(query: str) -> str:
    return compose(
        "exec", "-T", "postgres", "psql", "-U", "litellm", "-d", "governance", "-tAc", query
    ).strip()


def api(method: str, path: str, user: str, body: dict[str, Any] | None = None) -> httpx.Response:
    return httpx.request(
        method,
        f"{LITELLM_URL}/governance{path}",
        headers={"Authorization": f"Bearer {user_token(user)}"},
        json=body,
        timeout=30,
    )


async def freeze(
    amount: float, *, user: str = "alice", key: str = "kyt-agent@1.4", counterparty: str = CLEAN
) -> ToolResult:
    arguments = {
        "account_id": f"acct-{RUN}",
        "amount_usd": amount,
        "counterparty": counterparty,
        "reason": "demo",
    }
    return await call_tool(key, user, "freeze_account", arguments)


def describe(result: ToolResult) -> str:
    body = result.body
    return f"{result.status} {body.get('reasons', '')} {body.get('approval_id', '')}".strip()


async def main() -> int:
    watch = await call_tool(
        "kyt-agent@1.4",
        "alice",
        "add_to_watchlist",
        {"account_id": f"acct-{RUN}", "reason": "demo"},
    )
    check("1 watchlist 放行", watch.status == "allowed", describe(watch))

    small = await freeze(5000)
    check("2 小額凍結放行", small.status == "allowed", describe(small))

    first = await freeze(25000)
    first_id = first.body.get("approval_id")
    approved = api("POST", f"/approvals/{first_id}/approve", "bob", {"comment": "demo"})
    redeemed = await freeze(25000)
    again = await freeze(25000)
    check(
        "3 核准後兌現一次",
        first.status == "pending_approval"
        and approved.status_code == 200
        and redeemed.status == "allowed"
        and again.status == "pending_approval"
        and again.body.get("approval_id") != first_id,
        f"{describe(first)} → 核准 {approved.status_code} → {redeemed.status} → {describe(again)}",
    )

    carol = await freeze(40000, user="carol")
    carol_self = api("POST", f"/approvals/{carol.body.get('approval_id')}/approve", "carol", {})
    check(
        "4 委託人不得自己核准",
        carol_self.status_code == 403
        and carol_self.json().get("detail") == "self_approval_forbidden",
        f"{carol_self.status_code} {carol_self.json().get('detail')}",
    )

    api("POST", f"/approvals/{again.body.get('approval_id')}/approve", "bob", {})
    changed = await freeze(26000)
    check(
        "5 核准後改參數需重新審批",
        changed.status == "pending_approval"
        and changed.body.get("approval_id") != again.body.get("approval_id"),
        describe(changed),
    )

    sanctioned = await freeze(10, counterparty=SANCTIONED)
    check(
        "6 制裁地址送審",
        sanctioned.status == "pending_approval"
        and "sanctioned_counterparty" in sanctioned.body.get("reasons", []),
        describe(sanctioned),
    )

    report = await call_tool(
        "kyt-agent@1.4", "alice", "file_str", {"account_id": f"acct-{RUN}", "summary": "demo"}
    )
    check(
        "7 STR 一律送審",
        report.status == "pending_approval"
        and report.body.get("reasons") == ["str_always_reviewed"],
        describe(report),
    )

    unregistered = await freeze(5000, key="kyt-agent@1.5")
    check(
        "8 未註冊版本被拒",
        unregistered.status == "denied"
        and unregistered.body.get("reasons") == ["unregistered_agent_version"],
        describe(unregistered),
    )

    pending = await freeze(30000)
    api("POST", f"/approvals/{pending.body.get('approval_id')}/approve", "bob", {})
    original = AGENTS_FILE.read_text()
    try:
        agents = json.loads(original)
        agents["kyt-agent"]["status"] = "suspended"
        AGENTS_FILE.write_text(json.dumps(agents, indent=2) + "\n")
        restart_opa()
        suspended = await freeze(30000)
    finally:
        AGENTS_FILE.write_text(original)
        restart_opa()
    check(
        "9 停用後核准無法兌現",
        suspended.status == "denied" and suspended.body.get("reasons") == ["agent_suspended"],
        describe(suspended),
    )

    no_role = await freeze(5000, user="bob")
    check(
        "10 委託人缺 analyst 角色",
        no_role.status == "denied" and no_role.body.get("reasons") == ["role_required"],
        describe(no_role),
    )

    compose("stop", "opa")
    try:
        offline = await freeze(5000)
    finally:
        compose("start", "opa")
        time.sleep(3)
    check(
        "11 OPA 停止時拒絕",
        offline.status == "denied" and offline.body.get("reasons") == ["policy_unavailable"],
        describe(offline),
    )

    rejected_id = changed.body.get("approval_id")
    api("POST", f"/approvals/{rejected_id}/reject", "bob", {"comment": "金額有誤"})
    resent = await freeze(26000)
    detail = api("GET", f"/approvals/{resent.body.get('approval_id')}", "bob").json()
    history = [(a["id"], a["status"]) for a in detail.get("history", [])]
    check(
        "12 駁回後重送產生新審批",
        resent.status == "pending_approval" and (rejected_id, "rejected") in history,
        f"{describe(resent)} history={history}",
    )

    before = api("GET", "/audit/verify", "bob").json()
    last_id, reasons = psql(
        "SELECT id, reasons::text FROM audit_events ORDER BY id DESC LIMIT 1"
    ).split("|", 1)
    psql(f"UPDATE audit_events SET reasons = '[\"tampered\"]'::jsonb WHERE id = {last_id}")
    try:
        tampered = api("GET", "/audit/verify", "bob").json()
    finally:
        psql(f"UPDATE audit_events SET reasons = $orig${reasons}$orig$::jsonb WHERE id = {last_id}")
    restored = api("GET", "/audit/verify", "bob").json()
    check(
        "13 稽核竄改可偵測",
        before["first_invalid_id"] is None
        and tampered["first_invalid_id"] == int(last_id)
        and restored["first_invalid_id"] is None,
        f"before={before} tampered={tampered['first_invalid_id']} "
        f"restored={restored['first_invalid_id']}",
    )

    print(f"\n{sum(results)}/{len(results)} 通過")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
