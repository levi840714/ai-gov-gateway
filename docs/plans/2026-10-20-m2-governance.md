# M2 治理服務 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 ai-gov-gateway 上實作 tool 呼叫的治理：agent 註冊與版本 key、OPA policy、maker-checker 審批、hash chain 稽核，並以腳本跑完 spec §9 的 13 個端到端情境。

**Architecture:** 治理邏輯是 `src/ai_gov_gateway/` 套件。LiteLLM 依 config 的 guardrail 設定載入 `ai_gov_gateway.plugin`，該模組在載入時把 `/governance` 路由掛到 LiteLLM 的 app，並提供 `pre_mcp_call` hook，所以 LiteLLM 與治理邏輯在同一個 process、同一個 port。引擎（`engine/`）不 import LiteLLM，依賴 `PolicyClient`（OPA）與 `GovernanceStore`（Postgres）兩個 Protocol，單元測試用 fake。

**Tech Stack:** Python 3.12+（映像內 3.13）、LiteLLM v1.105.1、FastAPI、psycopg 3、PyJWT、httpx、typer、OPA 1.21.1（Rego v1）、Keycloak 26.8.1、Postgres 17.6、`mcp` 1.30（只給腳本與 account_ops）、pytest + pytest-asyncio。

**Spec:** `docs/specs/2026-10-20-m2-governance-design.md`

**撰寫計畫前已實測的事實**（2026-10-20）
- LiteLLM 映像（Wolfi）沒有 `pip` 與 `uv`；venv 已有 fastapi 0.136.3、pydantic 2.13.4、pydantic-settings 2.14.2、httpx 0.28.1、PyJWT 2.15.0、cryptography 50.0.0、psycopg 3.3.3（binary），**`mcp` 是 2.2.0**，沒有 typer
- `get_instance_fn` 先找 config 目錄下的檔案，找不到才 `importlib` 匯入，所以 `ai_gov_gateway.plugin.GovernanceGuardrail` 可由 PYTHONPATH 載入
- 由 guardrail 模組在載入時 `app.include_router(...)`，以 LiteLLM 原本的 `litellm --config` 啟動後，自訂路由與 `/health/liveliness` 都可用
- LiteLLM CLI 在啟動 uvicorn 前會做 Prisma migration 與環境設定，所以**不**改用自訂 uvicorn 進入點
- OPA 載入目錄時，`<dir>/data.json` 對應到 `data.<dir>`
- 本計畫的 Rego 與 17 個 Rego 測試已在 OPA 1.21.1 執行通過，`opa fmt` 與 `opa check --strict` 無問題
- 參數不存在時，`not is_number(input.arguments.amount_usd)` 的結果是 undefined 而非 true（會 fail-open），必須用 `object.get` 給預設值

**通則**
- 指令都在 `ai-gov-gateway/` 目錄執行，除非另有說明
- 任何輸出都不得包含 token、API key、client secret 原文
- 每個 task 結束都要 `uv run ruff format src tests && uv run ruff check src tests && uv run mypy src` 無錯誤

---

## File Structure

| 檔案 | 職責 |
| ---- | ---- |
| `pyproject.toml` | 改為 src layout 套件（hatchling）；`scripts` 依賴群組放 `mcp<2` |
| `Dockerfile` | `FROM litellm/litellm:v1.105.1`，複製 `src/ai_gov_gateway`，設定 `PYTHONPATH` |
| `src/ai_gov_gateway/config.py` | `Settings`（`GOV_` 前綴環境變數） |
| `src/ai_gov_gateway/engine/models.py` | 資料模型：`ToolCall`、`PolicyDecision`、`Outcome`、`Approval`、`AuditEvent`、`AuditRecord` |
| `src/ai_gov_gateway/engine/clock.py` | `utc_now()` |
| `src/ai_gov_gateway/engine/fingerprint.py` | `canonical_json`、`call_fingerprint`、`parse_agent_key` |
| `src/ai_gov_gateway/engine/tokens.py` | `TokenVerifier`、`jwks_resolver` |
| `src/ai_gov_gateway/engine/audit.py` | hash chain：`build_record`、`first_invalid`、`approval_event` |
| `src/ai_gov_gateway/engine/store.py` | `GovernanceStore` Protocol、`StoreUnavailable`、`StoreError`、`transition_columns` |
| `src/ai_gov_gateway/engine/postgres.py` | `PostgresStore` |
| `src/ai_gov_gateway/engine/policy.py` | `PolicyClient` Protocol、`OpaPolicyClient`、`PolicyError` |
| `src/ai_gov_gateway/engine/engine.py` | `GovernanceEngine.decide()` |
| `src/ai_gov_gateway/engine/approvals.py` | `ApprovalService`：列表、詳情、核准、駁回、稽核驗證 |
| `src/ai_gov_gateway/runtime.py` | 由 `Settings` 組出 `Services`（lazy 單例） |
| `src/ai_gov_gateway/hook_adapter.py` | LiteLLM `data` → `ToolCall`（純函式） |
| `src/ai_gov_gateway/plugin.py` | 掛上路由、`GovernanceGuardrail`（只在 gateway 映像內 import） |
| `src/ai_gov_gateway/api/approvals.py` | `/governance` 路由 |
| `src/ai_gov_gateway/cli_auth.py` | device flow、token 檔案 |
| `src/ai_gov_gateway/cli.py` | `agw` 指令 |
| `policies/governance.rego`、`policies/governance_test.rego` | 規則與 Rego 測試 |
| `policies/{agents,tools,sanctions,meta}/data.json` | 註冊表、tool 風險、制裁名單、policy 版本 |
| `account_ops/state.py`、`account_ops/server.py`、`account_ops/Dockerfile` | 模擬下游 |
| `scripts/agent_sim.py`、`scripts/demo_m2.py` | 模擬 agent 呼叫、端到端情境 |
| `tests/fakes.py`、`tests/jwt_helpers.py` | 測試替身與測試用 JWT |
| `tests/test_*.py` | 單元測試 |
| `tests/integration/test_postgres_store.py`、`tests/integration/test_gateway_contract.py` | 整合測試 |

刪除：`litellm/governance_hook.py`（M1 寫死 freeze 規則的驗證版 hook，保留於 git 歷史）。

---

### Task 1: 把計畫前的實測結果寫回 spec

**Files:**
- Modify: `docs/specs/2026-10-20-m2-governance-design.md`

- [ ] **Step 1: 在 spec 結尾新增一節**

```markdown
## 11. 撰寫實作計畫時的修正（2026-10-20）

計畫前實測後，以下細節與上文不同，以本節為準：

| 項目 | 上文 | 修正 | 理由 |
| ---- | ---- | ---- | ---- |
| 啟動方式 | 自訂 uvicorn 進入點 `app.py` | 保留 `litellm --config` 啟動；LiteLLM 依 guardrail 設定載入 `ai_gov_gateway.plugin` 時，該模組把 `/governance` router 掛到 app | LiteLLM CLI 在啟動前會做 Prisma migration 與環境設定，自訂進入點會略過 |
| 映像 | `uv pip install` 套件 | 複製 `src/ai_gov_gateway` 並設定 `PYTHONPATH` | 映像內沒有 pip 與 uv；執行期相依已在 LiteLLM 的 venv |
| `mcp` 相依 | 未說明 | 只放在 `scripts` 依賴群組（`mcp<2`），不列入套件相依 | LiteLLM venv 內是 mcp 2.2.0 |
| Policy 資料位置 | `policies/data/agents.json` 等 | `policies/agents/data.json`、`policies/tools/data.json`、`policies/sanctions/data.json`、`policies/meta/data.json` | OPA 以 `<dir>/data.json` 對應 `data.<dir>` |
| 規則 | 規則 1–10 | 另加 `amount_missing`、`counterparty_missing`（凍結參數缺漏時送審）與 `high_risk_unreviewed_tool`（新增的高風險 tool 未寫規則時送審） | 參數缺漏時 Rego 的 `not` 會得到 undefined 而 fail-open；新 tool 預設送審 |
| 稽核欄位 | §7 | 新增 `comment` | 核准與駁回的意見需可追溯 |
| 稽核驗證 | `agw audit verify` 直接讀資料庫 | 經 `GET /governance/audit/verify`（`compliance_supervisor`） | CLI 不需資料庫憑證 |
| 寫入一致性 | 未說明 | 審批狀態變更與對應的稽核事件在同一個 transaction 寫入 | 避免「狀態變了但沒有稽核」 |
| account_ops 的 tool 鎖定 | 未說明 | M2 不設定 | 不在 M2 驗收情境中 |
| Postgres port | 未對外 | 開 `127.0.0.1:5433` | 主機上的整合測試需要連線 |
| 情境 4 | alice 自己核准 | 新增測試帳號 carol（analyst + compliance_supervisor），由 carol 委託後自己核准 | alice 沒有主管角色，只會得到 `role_required`，測不到 maker-checker |
```

- [ ] **Step 2: Commit**

```bash
git add docs/specs/2026-10-20-m2-governance-design.md
git commit -m "docs: record M2 design corrections found while planning"
```

---

### Task 2: 套件骨架與設定

**Files:**
- Modify: `pyproject.toml`
- Create: `src/ai_gov_gateway/__init__.py`、`src/ai_gov_gateway/engine/__init__.py`、`src/ai_gov_gateway/api/__init__.py`（皆為空檔）
- Create: `src/ai_gov_gateway/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: 改寫 `pyproject.toml`**

```toml
[project]
name = "ai-gov-gateway"
version = "0.2.0"
description = "Governance gateway for agent LLM traffic and tool calls"
requires-python = ">=3.12"
# gateway 映像以 PYTHONPATH 載入原始碼：除 typer（只給主機上的 CLI）外，須已存在於 LiteLLM 的 venv
dependencies = [
    "fastapi>=0.115",
    "httpx>=0.28",
    "psycopg[binary]>=3.2",
    "pydantic>=2.10",
    "pydantic-settings>=2.6",
    "pyjwt[crypto]>=2.10",
    "typer>=0.15",
]

[project.scripts]
agw = "ai_gov_gateway.cli:app"

[dependency-groups]
dev = ["mypy>=1.18", "pytest>=8.4", "pytest-asyncio>=0.24", "ruff>=0.13"]
# M1 驗證腳本、示範腳本與 account_ops 用；gateway 映像內是 mcp 2.x，不可列入套件相依
scripts = ["mcp>=1.30,<2", "python-dotenv>=1.1"]

[tool.uv]
default-groups = ["dev", "scripts"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/ai_gov_gateway"]

[tool.ruff]
line-length = 100
# docs 內的程式碼片段刻意保留縮排，不交給 ruff 格式化
extend-exclude = ["docs"]

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]

[tool.mypy]
python_version = "3.12"
disallow_untyped_defs = true
warn_unused_ignores = true

[[tool.mypy.overrides]]
module = ["litellm", "litellm.*"]
ignore_missing_imports = true

[tool.pytest.ini_options]
pythonpath = ["."]
asyncio_mode = "auto"
markers = ["integration: requires docker compose services"]
addopts = "-m 'not integration'"
```

- [ ] **Step 2: 寫失敗的測試 `tests/test_config.py`**

```python
from ai_gov_gateway.config import Settings


def test_defaults_point_at_compose_services(monkeypatch):
    for key in ("GOV_OPA_URL", "GOV_APPROVAL_TTL_HOURS", "GOV_OIDC_AUDIENCE"):
        monkeypatch.delenv(key, raising=False)
    settings = Settings()
    assert settings.opa_url == "http://opa:8181"
    assert settings.approval_ttl_hours == 24
    assert settings.oidc_audience == "ai-gateway"
    assert settings.approver_role == "compliance_supervisor"


def test_reads_gov_prefixed_environment(monkeypatch):
    monkeypatch.setenv("GOV_OPA_URL", "http://localhost:8181")
    monkeypatch.setenv("GOV_APPROVAL_TTL_HOURS", "2")
    settings = Settings()
    assert settings.opa_url == "http://localhost:8181"
    assert settings.approval_ttl_hours == 2
```

- [ ] **Step 3: 確認失敗**

Run: `uv sync && uv run pytest tests/test_config.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'ai_gov_gateway'` 或 `ai_gov_gateway.config`

- [ ] **Step 4: 實作 `src/ai_gov_gateway/config.py`**

```python
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """gateway 內治理元件的設定；容器以環境變數提供，不讀 .env。"""

    model_config = SettingsConfigDict(env_prefix="GOV_", extra="ignore")

    database_url: str = ""
    opa_url: str = "http://opa:8181"
    opa_timeout_seconds: float = 2.0
    oidc_issuer: str = "http://localhost:8080/realms/governance"
    oidc_jwks_url: str = "http://keycloak:8080/realms/governance/protocol/openid-connect/certs"
    oidc_audience: str = "ai-gateway"
    approval_ttl_hours: int = 24
    approver_role: str = "compliance_supervisor"
```

- [ ] **Step 5: 確認通過（含既有測試）並 Commit**

Run: `uv run pytest -q && uv run ruff format src tests && uv run ruff check src tests && uv run mypy src`
Expected: 6 passed（既有 4 + 新 2），無 lint / 型別錯誤

```bash
git add pyproject.toml uv.lock src tests/test_config.py
git commit -m "feat: scaffold ai_gov_gateway package and settings"
```

---

### Task 3: 資料模型與 fingerprint

**Files:**
- Create: `src/ai_gov_gateway/engine/models.py`、`src/ai_gov_gateway/engine/clock.py`、`src/ai_gov_gateway/engine/fingerprint.py`
- Test: `tests/test_fingerprint.py`

- [ ] **Step 1: 寫失敗的測試 `tests/test_fingerprint.py`**

```python
import json
from datetime import UTC, datetime

import pytest

from ai_gov_gateway.engine.fingerprint import call_fingerprint, canonical_json, parse_agent_key
from ai_gov_gateway.engine.models import AgentRef, Outcome

AGENT = AgentRef(id="kyt-agent", version="1.4", key_alias="kyt-agent@1.4")


def test_canonical_json_sorts_keys_without_spaces():
    assert canonical_json({"b": 1, "a": {"d": 2, "c": 3}}) == '{"a":{"c":3,"d":2},"b":1}'


def test_fingerprint_ignores_argument_order():
    first = call_fingerprint(AGENT, "alice", "account_ops", "freeze_account", {"a": 1, "b": 2})
    second = call_fingerprint(AGENT, "alice", "account_ops", "freeze_account", {"b": 2, "a": 1})
    assert first == second


def test_fingerprint_binds_arguments_user_and_version():
    base = call_fingerprint(AGENT, "alice", "s", "t", {"amount_usd": 1})
    assert base != call_fingerprint(AGENT, "alice", "s", "t", {"amount_usd": 2})
    assert base != call_fingerprint(AGENT, "bob", "s", "t", {"amount_usd": 1})
    newer = AgentRef(id="kyt-agent", version="1.5", key_alias="kyt-agent@1.5")
    assert base != call_fingerprint(newer, "alice", "s", "t", {"amount_usd": 1})


@pytest.mark.parametrize("alias", [None, "", "kyt-agent", "kyt-agent@", "@1.4", "a@b@c"])
def test_parse_agent_key_rejects_malformed(alias):
    assert parse_agent_key(alias) is None


def test_parse_agent_key():
    assert parse_agent_key("kyt-agent@1.4") == AGENT


def test_outcome_messages_are_json():
    expires = datetime(2026, 10, 21, 10, tzinfo=UTC)
    pending = Outcome.pending("apr_1", expires, ["amount_over_threshold"])
    assert json.loads(pending.to_message()) == {
        "governance": "pending_approval",
        "reasons": ["amount_over_threshold"],
        "approval_id": "apr_1",
        "expires_at": "2026-10-21T10:00:00+00:00",
    }
    denied = Outcome.denied(["role_required"])
    assert json.loads(denied.to_message()) == {"governance": "denied", "reasons": ["role_required"]}
```

- [ ] **Step 2: 確認失敗**

Run: `uv run pytest tests/test_fingerprint.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'ai_gov_gateway.engine.fingerprint'`

- [ ] **Step 3: 實作 `src/ai_gov_gateway/engine/models.py`**

```python
import json
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class Decision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    USED = "used"


class AgentRef(BaseModel):
    id: str
    version: str
    key_alias: str


class UserIdentity(BaseModel):
    sub: str
    username: str
    roles: list[str] = Field(default_factory=list)


class ToolCall(BaseModel):
    """hook 從 LiteLLM 取得的一次 tool 呼叫。"""

    key_alias: str | None
    user_token: str | None
    server: str
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class PolicyDecision(BaseModel):
    decision: Decision
    reasons: list[str]
    policy_version: str
    approver_role: str | None = None


OutcomeStatus = Literal["allowed", "denied", "pending_approval"]


class Outcome(BaseModel):
    status: OutcomeStatus
    reasons: list[str] = Field(default_factory=list)
    approval_id: str | None = None
    expires_at: datetime | None = None

    @classmethod
    def allowed(cls, reasons: list[str] | None = None) -> "Outcome":
        return cls(status="allowed", reasons=reasons or [])

    @classmethod
    def denied(cls, reasons: list[str]) -> "Outcome":
        return cls(status="denied", reasons=reasons)

    @classmethod
    def pending(cls, approval_id: str, expires_at: datetime, reasons: list[str]) -> "Outcome":
        return cls(
            status="pending_approval",
            reasons=reasons,
            approval_id=approval_id,
            expires_at=expires_at,
        )

    def to_message(self) -> str:
        """hook 阻擋時 raise 的訊息；agent 以 JSON 解析。"""
        body: dict[str, Any] = {"governance": self.status, "reasons": self.reasons}
        if self.approval_id is not None:
            body["approval_id"] = self.approval_id
        if self.expires_at is not None:
            body["expires_at"] = self.expires_at.isoformat()
        return json.dumps(body, ensure_ascii=False)


class Approval(BaseModel):
    id: str
    fingerprint: str
    status: ApprovalStatus
    agent: AgentRef
    user_sub: str
    username: str
    server: str
    tool: str
    arguments: dict[str, Any]
    reasons: list[str]
    approver_role: str
    created_at: datetime
    expires_at: datetime
    decided_by: str | None = None
    decided_by_username: str | None = None
    decided_at: datetime | None = None
    comment: str | None = None
    used_at: datetime | None = None


AuditEventType = Literal[
    "decision",
    "approval_requested",
    "approval_redeemed",
    "approval_approved",
    "approval_rejected",
    "approval_expired",
]


class AuditEvent(BaseModel):
    event_type: AuditEventType
    agent_id: str | None = None
    agent_version: str | None = None
    key_alias: str | None = None
    user_sub: str | None = None
    username: str | None = None
    actor_sub: str | None = None
    actor_username: str | None = None
    server: str | None = None
    tool: str | None = None
    arguments: dict[str, Any] | None = None
    decision: str | None = None
    reasons: list[str] = Field(default_factory=list)
    approval_id: str | None = None
    policy_version: str | None = None
    comment: str | None = None


class AuditRecord(AuditEvent):
    id: int
    ts: datetime
    prev_hash: str
    hash: str
```

- [ ] **Step 4: 實作 `clock.py` 與 `fingerprint.py`**

`src/ai_gov_gateway/engine/clock.py`：

```python
from collections.abc import Callable
from datetime import UTC, datetime

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)
```

`src/ai_gov_gateway/engine/fingerprint.py`：

```python
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
```

- [ ] **Step 5: 確認通過並 Commit**

Run: `uv run pytest tests/test_fingerprint.py -q && uv run ruff format src tests && uv run ruff check src tests && uv run mypy src`
Expected: 11 passed

```bash
git add src/ai_gov_gateway/engine tests/test_fingerprint.py
git commit -m "feat: add governance models and call fingerprint"
```

---

### Task 4: 使用者 token 驗證

**Files:**
- Create: `src/ai_gov_gateway/engine/tokens.py`
- Create: `tests/jwt_helpers.py`
- Test: `tests/test_tokens.py`

- [ ] **Step 1: 建立測試用 JWT 工具 `tests/jwt_helpers.py`**

```python
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from ai_gov_gateway.engine.tokens import TokenVerifier

ISSUER = "http://localhost:8080/realms/governance"
AUDIENCE = "ai-gateway"
_SIGNING_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_FOREIGN_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def make_token(
    username: str,
    roles: list[str],
    *,
    audience: str | list[str] = AUDIENCE,
    issuer: str = ISSUER,
    expires_in: int = 300,
    foreign: bool = False,
) -> str:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "iss": issuer,
        "aud": audience,
        "sub": f"{username}-sub",
        "preferred_username": username,
        "realm_access": {"roles": roles},
        "iat": now,
        "exp": now + timedelta(seconds=expires_in),
    }
    key = _FOREIGN_KEY if foreign else _SIGNING_KEY
    return jwt.encode(payload, key, algorithm="RS256")


def alice_token() -> str:
    return make_token("alice", ["analyst"])


def bob_token() -> str:
    return make_token("bob", ["compliance_supervisor"])


def carol_token() -> str:
    return make_token("carol", ["compliance_supervisor"])


def make_verifier() -> TokenVerifier:
    public_key = _SIGNING_KEY.public_key()
    return TokenVerifier(issuer=ISSUER, audience=AUDIENCE, key_resolver=lambda token: public_key)
```

- [ ] **Step 2: 寫失敗的測試 `tests/test_tokens.py`**

```python
import pytest

from ai_gov_gateway.engine.models import UserIdentity
from ai_gov_gateway.engine.tokens import TokenError
from tests.jwt_helpers import alice_token, make_token, make_verifier


def test_verify_returns_identity():
    assert make_verifier().verify(alice_token()) == UserIdentity(
        sub="alice-sub", username="alice", roles=["analyst"]
    )


def test_verify_accepts_audience_list():
    token = make_token("alice", ["analyst"], audience=["ai-gateway", "account"])
    assert make_verifier().verify(token).username == "alice"


@pytest.mark.parametrize(
    "token",
    [
        make_token("alice", ["analyst"], expires_in=-10),
        make_token("alice", ["analyst"], audience="other"),
        make_token("alice", ["analyst"], issuer="http://evil.test/realms/governance"),
        make_token("alice", ["analyst"], foreign=True),
        "not-a-jwt",
    ],
    ids=["expired", "audience", "issuer", "signature", "garbage"],
)
def test_verify_rejects_invalid_tokens(token):
    with pytest.raises(TokenError) as error:
        make_verifier().verify(token)
    assert token not in str(error.value)
```

- [ ] **Step 3: 確認失敗**

Run: `uv run pytest tests/test_tokens.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'ai_gov_gateway.engine.tokens'`

- [ ] **Step 4: 實作 `src/ai_gov_gateway/engine/tokens.py`**

```python
from collections.abc import Callable
from typing import Any

import jwt

from ai_gov_gateway.engine.models import UserIdentity

KeyResolver = Callable[[str], Any]


class TokenError(Exception):
    """token 無法驗證；訊息只含錯誤類型，不含 token。"""


class TokenVerifier:
    def __init__(self, *, issuer: str, audience: str, key_resolver: KeyResolver) -> None:
        self._issuer = issuer
        self._audience = audience
        self._key_resolver = key_resolver

    def verify(self, token: str) -> UserIdentity:
        try:
            claims = jwt.decode(
                token,
                self._key_resolver(token),
                algorithms=["RS256"],
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as error:
            raise TokenError(type(error).__name__) from error
        realm_access = claims.get("realm_access") or {}
        return UserIdentity(
            sub=str(claims["sub"]),
            username=str(claims.get("preferred_username") or claims["sub"]),
            roles=[str(role) for role in realm_access.get("roles", [])],
        )


def jwks_resolver(jwks_url: str) -> KeyResolver:
    client = jwt.PyJWKClient(jwks_url, cache_keys=True)

    def resolve(token: str) -> Any:
        return client.get_signing_key_from_jwt(token).key

    return resolve
```

`"not-a-jwt"` 在 `self._key_resolver(token)` 之前不會被解析；測試的 resolver 直接回傳公鑰，由 `jwt.decode` 拋出 `DecodeError`。正式環境的 `PyJWKClient` 對壞 token 拋 `PyJWKClientError`（同為 `PyJWTError` 子類）。

- [ ] **Step 5: 確認通過並 Commit**

Run: `uv run pytest tests/test_tokens.py -q && uv run ruff format src tests && uv run ruff check src tests && uv run mypy src`
Expected: 7 passed

```bash
git add src/ai_gov_gateway/engine/tokens.py tests/jwt_helpers.py tests/test_tokens.py
git commit -m "feat: verify user tokens against the IdP signing keys"
```

---

### Task 5: 稽核 hash chain

**Files:**
- Create: `src/ai_gov_gateway/engine/audit.py`
- Test: `tests/test_audit.py`

- [ ] **Step 1: 寫失敗的測試 `tests/test_audit.py`**

```python
from datetime import UTC, datetime, timedelta, timezone

from ai_gov_gateway.engine.audit import (
    GENESIS_HASH,
    approval_event,
    build_record,
    first_invalid,
    record_hash,
)
from ai_gov_gateway.engine.models import AgentRef, Approval, ApprovalStatus, AuditEvent, UserIdentity

START = datetime(2026, 10, 20, 9, tzinfo=UTC)


def _event(n: int) -> AuditEvent:
    return AuditEvent(event_type="decision", tool="t", reasons=[f"r{n}"], arguments={"n": n})


def _chain(count: int):
    records, prev = [], GENESIS_HASH
    for n in range(1, count + 1):
        record = build_record(
            record_id=n, prev_hash=prev, ts=START + timedelta(seconds=n), event=_event(n)
        )
        records.append(record)
        prev = record.hash
    return records


def test_valid_chain_has_no_invalid_record():
    assert first_invalid(_chain(3)) is None


def test_tampered_field_is_reported():
    records = _chain(3)
    records[1] = records[1].model_copy(update={"username": "mallory"})
    assert first_invalid(records) == 2


def test_broken_link_is_reported():
    records = _chain(3)
    records[2] = records[2].model_copy(update={"prev_hash": GENESIS_HASH})
    assert first_invalid(records) == 3


def test_hash_is_independent_of_timezone_representation():
    taipei = START.astimezone(timezone(timedelta(hours=8)))
    assert record_hash(GENESIS_HASH, START, _event(1)) == record_hash(GENESIS_HASH, taipei, _event(1))


def test_approval_event_copies_approval_fields():
    approval = Approval(
        id="apr_1",
        fingerprint="f",
        status=ApprovalStatus.APPROVED,
        agent=AgentRef(id="kyt-agent", version="1.4", key_alias="kyt-agent@1.4"),
        user_sub="alice-sub",
        username="alice",
        server="account_ops",
        tool="freeze_account",
        arguments={"amount_usd": 25000},
        reasons=["amount_over_threshold"],
        approver_role="compliance_supervisor",
        created_at=START,
        expires_at=START + timedelta(hours=24),
    )
    bob = UserIdentity(sub="bob-sub", username="bob", roles=["compliance_supervisor"])
    event = approval_event("approval_approved", approval, actor=bob, decision="approved", comment="ok")
    assert event.agent_version == "1.4"
    assert event.actor_username == "bob"
    assert event.approval_id == "apr_1"
    assert event.comment == "ok"
    assert event.arguments == {"amount_usd": 25000}
```

- [ ] **Step 2: 確認失敗**

Run: `uv run pytest tests/test_audit.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'ai_gov_gateway.engine.audit'`

- [ ] **Step 3: 實作 `src/ai_gov_gateway/engine/audit.py`**

```python
from collections.abc import Sequence
from datetime import UTC, datetime

from ai_gov_gateway.engine.fingerprint import canonical_json, sha256_hex
from ai_gov_gateway.engine.models import (
    Approval,
    AuditEvent,
    AuditEventType,
    AuditRecord,
    UserIdentity,
)

GENESIS_HASH = "0" * 64


def record_hash(prev_hash: str, ts: datetime, event: AuditEvent) -> str:
    # 資料庫讀回的時間可能帶不同時區，統一換成 UTC 再算
    body = {
        "prev_hash": prev_hash,
        "ts": ts.astimezone(UTC).isoformat(),
        "event": event.model_dump(mode="json"),
    }
    return sha256_hex(canonical_json(body))


def build_record(*, record_id: int, prev_hash: str, ts: datetime, event: AuditEvent) -> AuditRecord:
    return AuditRecord(
        id=record_id,
        ts=ts,
        prev_hash=prev_hash,
        hash=record_hash(prev_hash, ts, event),
        **event.model_dump(),
    )


def event_of(record: AuditRecord) -> AuditEvent:
    return AuditEvent.model_validate(record.model_dump(include=set(AuditEvent.model_fields)))


def first_invalid(records: Sequence[AuditRecord]) -> int | None:
    """回傳第一筆 hash 對不上的 id；整條 chain 正確時回傳 None。"""
    prev = GENESIS_HASH
    for record in records:
        if record.prev_hash != prev or record_hash(prev, record.ts, event_of(record)) != record.hash:
            return record.id
        prev = record.hash
    return None


def approval_event(
    event_type: AuditEventType,
    approval: Approval,
    *,
    actor: UserIdentity | None = None,
    decision: str | None = None,
    comment: str | None = None,
) -> AuditEvent:
    return AuditEvent(
        event_type=event_type,
        agent_id=approval.agent.id,
        agent_version=approval.agent.version,
        key_alias=approval.agent.key_alias,
        user_sub=approval.user_sub,
        username=approval.username,
        actor_sub=actor.sub if actor else None,
        actor_username=actor.username if actor else None,
        server=approval.server,
        tool=approval.tool,
        arguments=approval.arguments,
        decision=decision,
        reasons=approval.reasons,
        approval_id=approval.id,
        comment=comment,
    )
```

- [ ] **Step 4: 確認通過並 Commit**

Run: `uv run pytest tests/test_audit.py -q && uv run ruff format src tests && uv run ruff check src tests && uv run mypy src`
Expected: 5 passed

```bash
git add src/ai_gov_gateway/engine/audit.py tests/test_audit.py
git commit -m "feat: add hash-chained audit records"
```

---

### Task 6: Store Protocol 與測試替身

**Files:**
- Create: `src/ai_gov_gateway/engine/store.py`
- Create: `tests/fakes.py`
- Test: `tests/test_fakes.py`

- [ ] **Step 1: 實作 `src/ai_gov_gateway/engine/store.py`**

```python
from datetime import datetime
from typing import Any, Protocol

from ai_gov_gateway.engine.models import (
    Approval,
    ApprovalStatus,
    AuditEvent,
    AuditRecord,
    UserIdentity,
)


class StoreUnavailable(Exception):
    """資料庫連不上。"""


class StoreError(Exception):
    """資料庫操作失敗（連線以外的原因）。"""


class GovernanceStore(Protocol):
    async def open_approval(self, fingerprint: str) -> Approval | None:
        """同一 fingerprint 中最新一筆 pending 或 approved 的審批。"""
        ...

    async def approvals_for(self, fingerprint: str) -> list[Approval]: ...

    async def get_approval(self, approval_id: str) -> Approval | None: ...

    async def list_approvals(self, status: ApprovalStatus | None) -> list[Approval]: ...

    async def create_approval(self, approval: Approval, audit: AuditEvent) -> AuditRecord:
        """建立審批並在同一個 transaction 寫入稽核。"""
        ...

    async def transition(
        self,
        approval_id: str,
        *,
        from_statuses: tuple[ApprovalStatus, ...],
        to: ApprovalStatus,
        at: datetime,
        actor: UserIdentity | None = None,
        comment: str | None = None,
        audit: AuditEvent | None = None,
    ) -> bool:
        """狀態在 from_statuses 中才轉換；轉換與稽核同一個 transaction，回傳是否成功。"""
        ...

    async def append_audit(self, event: AuditEvent, ts: datetime) -> AuditRecord: ...

    async def audit_records(self) -> list[AuditRecord]: ...


def transition_columns(
    to: ApprovalStatus, at: datetime, actor: UserIdentity | None, comment: str | None
) -> dict[str, Any]:
    columns: dict[str, Any] = {"status": to}
    if to in (ApprovalStatus.APPROVED, ApprovalStatus.REJECTED):
        columns |= {
            "decided_by": actor.sub if actor else None,
            "decided_by_username": actor.username if actor else None,
            "decided_at": at,
            "comment": comment,
        }
    if to is ApprovalStatus.USED:
        columns["used_at"] = at
    return columns
```

- [ ] **Step 2: 建立 `tests/fakes.py`**

```python
from datetime import UTC, datetime, timedelta
from itertools import count
from typing import Any

from ai_gov_gateway.engine.audit import GENESIS_HASH, build_record
from ai_gov_gateway.engine.models import (
    Approval,
    ApprovalStatus,
    AuditEvent,
    AuditRecord,
    Decision,
    PolicyDecision,
    UserIdentity,
)
from ai_gov_gateway.engine.policy import PolicyError
from ai_gov_gateway.engine.store import transition_columns

OPEN = (ApprovalStatus.PENDING, ApprovalStatus.APPROVED)


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 20, 9, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


def id_sequence() -> Any:
    numbers = count(1)
    return lambda: f"apr_{next(numbers)}"


class InMemoryStore:
    def __init__(self) -> None:
        self.approvals: dict[str, Approval] = {}
        self.audit: list[AuditRecord] = []
        self.fail: Exception | None = None

    def _check(self) -> None:
        if self.fail is not None:
            raise self.fail

    def _append(self, event: AuditEvent, ts: datetime) -> AuditRecord:
        prev = self.audit[-1].hash if self.audit else GENESIS_HASH
        record = build_record(record_id=len(self.audit) + 1, prev_hash=prev, ts=ts, event=event)
        self.audit.append(record)
        return record

    async def open_approval(self, fingerprint: str) -> Approval | None:
        self._check()
        matches = [
            a for a in self.approvals.values() if a.fingerprint == fingerprint and a.status in OPEN
        ]
        return max(matches, key=lambda a: a.created_at, default=None)

    async def approvals_for(self, fingerprint: str) -> list[Approval]:
        self._check()
        matches = [a for a in self.approvals.values() if a.fingerprint == fingerprint]
        return sorted(matches, key=lambda a: a.created_at)

    async def get_approval(self, approval_id: str) -> Approval | None:
        self._check()
        return self.approvals.get(approval_id)

    async def list_approvals(self, status: ApprovalStatus | None) -> list[Approval]:
        self._check()
        rows = [a for a in self.approvals.values() if status is None or a.status is status]
        return sorted(rows, key=lambda a: a.created_at, reverse=True)

    async def create_approval(self, approval: Approval, audit: AuditEvent) -> AuditRecord:
        self._check()
        self.approvals[approval.id] = approval
        return self._append(audit, approval.created_at)

    async def transition(
        self,
        approval_id: str,
        *,
        from_statuses: tuple[ApprovalStatus, ...],
        to: ApprovalStatus,
        at: datetime,
        actor: UserIdentity | None = None,
        comment: str | None = None,
        audit: AuditEvent | None = None,
    ) -> bool:
        self._check()
        approval = self.approvals.get(approval_id)
        if approval is None or approval.status not in from_statuses:
            return False
        update = transition_columns(to, at, actor, comment)
        self.approvals[approval_id] = approval.model_copy(update=update)
        if audit is not None:
            self._append(audit, at)
        return True

    async def append_audit(self, event: AuditEvent, ts: datetime) -> AuditRecord:
        self._check()
        return self._append(event, ts)

    async def audit_records(self) -> list[AuditRecord]:
        self._check()
        return list(self.audit)


def policy_result(decision: Decision, *reasons: str) -> PolicyDecision:
    approver = "compliance_supervisor" if decision is Decision.REQUIRE_APPROVAL else None
    return PolicyDecision(
        decision=decision, reasons=list(reasons), policy_version="test-1", approver_role=approver
    )


class FakePolicy:
    def __init__(self, decision: PolicyDecision | None = None) -> None:
        self.decision = decision or policy_result(Decision.ALLOW)
        self.error: str | None = None
        self.inputs: list[dict[str, Any]] = []

    async def evaluate(self, policy_input: dict[str, Any]) -> PolicyDecision:
        self.inputs.append(policy_input)
        if self.error is not None:
            raise PolicyError(self.error)
        return self.decision
```

`tests/fakes.py` 會 import `ai_gov_gateway.engine.policy`，所以 Step 3 的測試要等 Task 7 完成才能通過；本 task 先只 commit store。

- [ ] **Step 3: 確認 store 可匯入並 Commit**

Run: `uv run python -c "import ai_gov_gateway.engine.store" && uv run ruff format src tests && uv run ruff check src && uv run mypy src`
Expected: 無錯誤

```bash
git add src/ai_gov_gateway/engine/store.py tests/fakes.py
git commit -m "feat: define governance store protocol and in-memory fake"
```

---

### Task 7: OPA client

**Files:**
- Create: `src/ai_gov_gateway/engine/policy.py`
- Test: `tests/test_policy_client.py`、`tests/test_fakes.py`

- [ ] **Step 1: 寫失敗的測試 `tests/test_policy_client.py`**

```python
import json

import httpx
import pytest

from ai_gov_gateway.engine.models import Decision
from ai_gov_gateway.engine.policy import OpaPolicyClient, PolicyError


def _client(handler) -> OpaPolicyClient:
    return OpaPolicyClient("http://opa:8181", 2.0, transport=httpx.MockTransport(handler))


async def test_posts_input_and_parses_decision():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        result = {
            "decision": "require_approval",
            "reasons": ["amount_over_threshold"],
            "approver_role": "compliance_supervisor",
            "policy_version": "2026-10-20.1",
        }
        return httpx.Response(200, json={"result": result})

    decision = await _client(handler).evaluate({"tool": {"name": "freeze_account"}})
    assert seen == {
        "path": "/v1/data/governance/decision",
        "body": {"input": {"tool": {"name": "freeze_account"}}},
    }
    assert decision.decision is Decision.REQUIRE_APPROVAL
    assert decision.policy_version == "2026-10-20.1"


def _raise_connect(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


@pytest.mark.parametrize(
    ("handler", "code"),
    [
        (_raise_connect, "policy_unavailable"),
        (lambda request: httpx.Response(500, json={}), "policy_unavailable"),
        (lambda request: httpx.Response(200, json={}), "policy_error"),
        (lambda request: httpx.Response(200, json={"result": {"decision": "maybe"}}), "policy_error"),
        (lambda request: httpx.Response(200, text="not json"), "policy_error"),
    ],
    ids=["connect", "server-error", "no-result", "bad-decision", "not-json"],
)
async def test_failures_map_to_codes(handler, code):
    with pytest.raises(PolicyError) as error:
        await _client(handler).evaluate({})
    assert error.value.code == code
```

- [ ] **Step 2: 確認失敗**

Run: `uv run pytest tests/test_policy_client.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'ai_gov_gateway.engine.policy'`

- [ ] **Step 3: 實作 `src/ai_gov_gateway/engine/policy.py`**

```python
from typing import Any, Protocol

import httpx

from ai_gov_gateway.engine.models import PolicyDecision


class PolicyError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class PolicyClient(Protocol):
    async def evaluate(self, policy_input: dict[str, Any]) -> PolicyDecision: ...


class OpaPolicyClient:
    DECISION_PATH = "/v1/data/governance/decision"

    def __init__(
        self, base_url: str, timeout: float, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._base_url = base_url
        self._timeout = timeout
        self._transport = transport

    async def evaluate(self, policy_input: dict[str, Any]) -> PolicyDecision:
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url, timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.post(self.DECISION_PATH, json={"input": policy_input})
                response.raise_for_status()
        except httpx.HTTPError as error:
            raise PolicyError("policy_unavailable") from error
        try:
            body = response.json()
            result = body.get("result") if isinstance(body, dict) else None
            return PolicyDecision.model_validate(result)
        except ValueError as error:
            raise PolicyError("policy_error") from error
```

`pydantic.ValidationError` 是 `ValueError` 的子類，`response.json()` 解析失敗也是 `ValueError`。

- [ ] **Step 4: 補上替身的測試 `tests/test_fakes.py`**

```python
from datetime import timedelta

from ai_gov_gateway.engine.audit import first_invalid
from ai_gov_gateway.engine.models import AgentRef, Approval, ApprovalStatus, AuditEvent
from tests.fakes import FakeClock, InMemoryStore


def _approval(clock: FakeClock) -> Approval:
    return Approval(
        id="apr_1",
        fingerprint="f",
        status=ApprovalStatus.PENDING,
        agent=AgentRef(id="kyt-agent", version="1.4", key_alias="kyt-agent@1.4"),
        user_sub="alice-sub",
        username="alice",
        server="account_ops",
        tool="freeze_account",
        arguments={},
        reasons=[],
        approver_role="compliance_supervisor",
        created_at=clock(),
        expires_at=clock() + timedelta(hours=24),
    )


async def test_transition_only_from_allowed_statuses():
    clock, store = FakeClock(), InMemoryStore()
    await store.create_approval(_approval(clock), AuditEvent(event_type="approval_requested"))
    used = await store.transition(
        "apr_1", from_statuses=(ApprovalStatus.APPROVED,), to=ApprovalStatus.USED, at=clock()
    )
    assert used is False
    approved = await store.transition(
        "apr_1",
        from_statuses=(ApprovalStatus.PENDING,),
        to=ApprovalStatus.APPROVED,
        at=clock(),
        audit=AuditEvent(event_type="approval_approved"),
    )
    assert approved is True
    assert (await store.get_approval("apr_1")).status is ApprovalStatus.APPROVED
    assert [r.event_type for r in store.audit] == ["approval_requested", "approval_approved"]
    assert first_invalid(store.audit) is None
```

- [ ] **Step 5: 確認通過並 Commit**

Run: `uv run pytest tests/test_policy_client.py tests/test_fakes.py -q && uv run ruff format src tests && uv run ruff check src tests && uv run mypy src`
Expected: 7 passed

```bash
git add src/ai_gov_gateway/engine/policy.py tests/test_policy_client.py tests/test_fakes.py
git commit -m "feat: add OPA policy client with fail-closed error codes"
```

---

### Task 8: OPA policy 與資料

**Files:**
- Create: `policies/governance.rego`、`policies/governance_test.rego`
- Create: `policies/agents/data.json`、`policies/tools/data.json`、`policies/sanctions/data.json`、`policies/meta/data.json`

以下內容已在撰寫計畫時以 OPA 1.21.1 執行 `opa test`（17/17）、`opa fmt --list`、`opa check --strict` 通過。

- [ ] **Step 1: 資料檔**

`policies/agents/data.json`：

```json
{
  "kyt-agent": {
    "owner": "compliance-engineering",
    "status": "active",
    "max_risk": "high",
    "versions": {
      "1.4": {"key_alias": "kyt-agent@1.4", "approved_by": "governance-team", "approved_at": "2026-10-20"}
    }
  }
}
```

`policies/tools/data.json`：

```json
{
  "account_ops.add_to_watchlist": {"risk": "low"},
  "account_ops.freeze_account": {"risk": "high", "amount_threshold_usd": 10000},
  "account_ops.file_str": {"risk": "high"},
  "probe.whoami": {"risk": "low"},
  "probe.echo": {"risk": "low"}
}
```

`policies/sanctions/data.json`（範例地址，小寫）：

```json
{"addresses": ["0x8589427373d6d84e98730d7795d8f6f8731fda16"]}
```

`policies/meta/data.json`：

```json
{"policy_version": "2026-10-20.1"}
```

- [ ] **Step 2: `policies/governance.rego`**

```rego
package governance

# 治理團隊維護：規則依序判斷，第一個符合的 deny 原因即為結果
risk_rank := {"low": 1, "high": 2}

approver_role := "compliance_supervisor"

tool_key := sprintf("%s.%s", [input.tool.server, input.tool.name])

agent := data.agents[input.agent.id]

tool := data.tools[tool_key]

# 已有明確審批規則的高風險 tool；新增高風險 tool 而未寫規則時一律送審
reviewed_high_risk := {"account_ops.freeze_account", "account_ops.file_str"}

registered if agent.versions[input.agent.version].key_alias == input.agent.key_alias

deny_reason := "unregistered_agent_version" if {
	not registered
} else := "agent_suspended" if {
	agent.status != "active"
} else := "unknown_tool" if {
	not tool
} else := "risk_exceeds_agent_limit" if {
	risk_rank[tool.risk] > risk_rank[agent.max_risk]
} else := "role_required" if {
	input.tool.server == "account_ops"
	not "analyst" in input.user.roles
}

approval_reasons contains "sanctioned_counterparty" if {
	tool_key == "account_ops.freeze_account"
	lower(input.arguments.counterparty) in data.sanctions.addresses
}

approval_reasons contains "amount_over_threshold" if {
	tool_key == "account_ops.freeze_account"
	input.arguments.amount_usd >= tool.amount_threshold_usd
}

# 參數不存在時引用結果為 undefined，not 也會是 undefined；以 object.get 給預設值才能判斷缺漏
approval_reasons contains "amount_missing" if {
	tool_key == "account_ops.freeze_account"
	not is_number(object.get(input.arguments, "amount_usd", null))
}

approval_reasons contains "counterparty_missing" if {
	tool_key == "account_ops.freeze_account"
	not is_string(object.get(input.arguments, "counterparty", null))
}

approval_reasons contains "str_always_reviewed" if {
	tool_key == "account_ops.file_str"
}

approval_reasons contains "high_risk_unreviewed_tool" if {
	tool.risk == "high"
	not tool_key in reviewed_high_risk
}

decision := {
	"decision": "deny",
	"reasons": [deny_reason],
	"policy_version": data.meta.policy_version,
} if {
	deny_reason
} else := {
	"decision": "require_approval",
	"reasons": sort(approval_reasons),
	"approver_role": approver_role,
	"policy_version": data.meta.policy_version,
} if {
	count(approval_reasons) > 0
} else := {
	"decision": "allow",
	"reasons": [],
	"policy_version": data.meta.policy_version,
}
```

- [ ] **Step 3: `policies/governance_test.rego`**

```rego
package governance_test

import data.governance

alice := {"sub": "alice-sub", "username": "alice", "roles": ["analyst"]}

bob := {"sub": "bob-sub", "username": "bob", "roles": ["compliance_supervisor"]}

agent_14 := {"id": "kyt-agent", "version": "1.4", "key_alias": "kyt-agent@1.4"}

call(tool, arguments) := {
	"agent": agent_14,
	"user": alice,
	"tool": {"server": "account_ops", "name": tool},
	"arguments": arguments,
}

freeze(amount, counterparty) := call("freeze_account", {
	"account_id": "acct-001",
	"amount_usd": amount,
	"counterparty": counterparty,
	"reason": "test",
})

clean := "0x1111111111111111111111111111111111111111"

sanctioned := "0x8589427373D6D84E98730D7795D8F6F8731FDA16"

test_watchlist_allowed if {
	result := governance.decision with input as call("add_to_watchlist", {"account_id": "a", "reason": "r"})
	result.decision == "allow"
	result.policy_version == "2026-10-20.1"
}

test_small_freeze_allowed if {
	governance.decision.decision == "allow" with input as freeze(5000, clean)
}

test_large_freeze_requires_approval if {
	result := governance.decision with input as freeze(25000, clean)
	result.decision == "require_approval"
	result.reasons == ["amount_over_threshold"]
	result.approver_role == "compliance_supervisor"
}

test_sanctioned_counterparty_requires_approval_case_insensitive if {
	result := governance.decision with input as freeze(10, sanctioned)
	result.reasons == ["sanctioned_counterparty"]
}

test_sanctioned_and_large_lists_both_reasons if {
	result := governance.decision with input as freeze(25000, sanctioned)
	result.reasons == ["amount_over_threshold", "sanctioned_counterparty"]
}

test_missing_amount_requires_approval if {
	input_doc := call("freeze_account", {"account_id": "a", "counterparty": clean, "reason": "r"})
	result := governance.decision with input as input_doc
	result.reasons == ["amount_missing"]
}

test_missing_counterparty_requires_approval if {
	input_doc := call("freeze_account", {"account_id": "a", "amount_usd": 10, "reason": "r"})
	result := governance.decision with input as input_doc
	result.reasons == ["counterparty_missing"]
}

test_str_always_reviewed if {
	result := governance.decision with input as call("file_str", {"account_id": "a", "summary": "s"})
	result.reasons == ["str_always_reviewed"]
}

test_unregistered_version_denied if {
	input_doc := object.union(freeze(5000, clean), {"agent": {"id": "kyt-agent", "version": "1.5", "key_alias": "kyt-agent@1.5"}})
	result := governance.decision with input as input_doc
	result.decision == "deny"
	result.reasons == ["unregistered_agent_version"]
}

test_unknown_agent_denied if {
	input_doc := object.union(freeze(5000, clean), {"agent": {"id": "other", "version": "1", "key_alias": "other@1"}})
	governance.decision.reasons == ["unregistered_agent_version"] with input as input_doc
}

test_suspended_agent_denied if {
	suspended := object.union(data.agents, {"kyt-agent": {"status": "suspended"}})
	result := governance.decision with input as freeze(25000, clean) with data.agents as suspended
	result.reasons == ["agent_suspended"]
}

test_unknown_tool_denied if {
	governance.decision.reasons == ["unknown_tool"] with input as call("delete_account", {})
}

test_risk_exceeds_agent_limit if {
	limited := object.union(data.agents, {"kyt-agent": {"max_risk": "low"}})
	result := governance.decision with input as freeze(5000, clean) with data.agents as limited
	result.reasons == ["risk_exceeds_agent_limit"]
}

test_role_required_for_account_ops if {
	input_doc := object.union(freeze(5000, clean), {"user": bob})
	governance.decision.reasons == ["role_required"] with input as input_doc
}

test_probe_allowed_without_analyst_role if {
	input_doc := {"agent": agent_14, "user": bob, "tool": {"server": "probe", "name": "whoami"}, "arguments": {}}
	governance.decision.decision == "allow" with input as input_doc
}

test_new_high_risk_tool_defaults_to_review if {
	tools := object.union(data.tools, {"account_ops.close_account": {"risk": "high"}})
	result := governance.decision with input as call("close_account", {}) with data.tools as tools
	result.reasons == ["high_risk_unreviewed_tool"]
}

test_policy_version_present if {
	is_string(data.meta.policy_version)
	data.meta.policy_version != ""
}
```

- [ ] **Step 4: 執行 Rego 測試、格式與嚴格檢查**

Run:
```bash
docker run --rm -v "$PWD/policies:/policies:ro" openpolicyagent/opa:1.21.1 test /policies
docker run --rm -v "$PWD/policies:/policies:ro" openpolicyagent/opa:1.21.1 fmt --list /policies
docker run --rm -v "$PWD/policies:/policies:ro" openpolicyagent/opa:1.21.1 check --strict /policies
```
Expected: `PASS: 33/33`；`fmt --list` 無輸出；`check` 無錯誤

- [ ] **Step 5: Commit**

```bash
git add policies
git commit -m "feat: add governance policy, agent registry and Rego tests"
```

---

### Task 9: 治理引擎

**Files:**
- Create: `src/ai_gov_gateway/engine/engine.py`
- Test: `tests/test_engine.py`

- [ ] **Step 1: 寫失敗的測試 `tests/test_engine.py`**

```python
from datetime import timedelta

import pytest

from ai_gov_gateway.engine.audit import first_invalid
from ai_gov_gateway.engine.engine import GovernanceEngine
from ai_gov_gateway.engine.models import ApprovalStatus, Decision, ToolCall
from ai_gov_gateway.engine.store import StoreError, StoreUnavailable
from tests.fakes import FakeClock, FakePolicy, InMemoryStore, id_sequence, policy_result
from tests.jwt_helpers import alice_token, make_token, make_verifier

NEEDS_APPROVAL = policy_result(Decision.REQUIRE_APPROVAL, "amount_over_threshold")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture
def policy() -> FakePolicy:
    return FakePolicy()


@pytest.fixture
def engine(clock, store, policy) -> GovernanceEngine:
    return GovernanceEngine(
        verifier=make_verifier(),
        policy=policy,
        store=store,
        approval_ttl=timedelta(hours=24),
        approver_role="compliance_supervisor",
        clock=clock,
        new_id=id_sequence(),
    )


def freeze(amount: int = 25000, *, key: str | None = "kyt-agent@1.4", token=None) -> ToolCall:
    return ToolCall(
        key_alias=key,
        user_token=alice_token() if token is None else token,
        server="account_ops",
        tool="freeze_account",
        arguments={"account_id": "acct-001", "amount_usd": amount, "counterparty": "0x1"},
    )


async def test_allow_is_audited_with_policy_input(engine, store, policy):
    outcome = await engine.decide(freeze(5000))
    assert outcome.status == "allowed"
    assert policy.inputs[0]["agent"] == {"id": "kyt-agent", "version": "1.4", "key_alias": "kyt-agent@1.4"}
    assert policy.inputs[0]["user"]["roles"] == ["analyst"]
    assert policy.inputs[0]["tool"] == {"server": "account_ops", "name": "freeze_account"}
    [record] = store.audit
    assert (record.event_type, record.decision, record.policy_version) == ("decision", "allowed", "test-1")
    assert record.username == "alice"


async def test_policy_deny_is_returned_and_audited(engine, store, policy):
    policy.decision = policy_result(Decision.DENY, "role_required")
    outcome = await engine.decide(freeze())
    assert (outcome.status, outcome.reasons) == ("denied", ["role_required"])
    assert store.audit[0].reasons == ["role_required"]


@pytest.mark.parametrize(
    ("call_kwargs", "reason"),
    [
        ({"token": ""}, "missing_user_token"),
        ({"token": make_token("alice", ["analyst"], expires_in=-10)}, "invalid_user_token"),
        ({"key": "kyt-agent-mcp"}, "unregistered_agent_version"),
        ({"key": None}, "unregistered_agent_version"),
    ],
)
async def test_rejected_before_policy(engine, store, policy, call_kwargs, reason):
    outcome = await engine.decide(freeze(**call_kwargs))
    assert (outcome.status, outcome.reasons) == ("denied", [reason])
    assert policy.inputs == []
    assert store.audit[0].reasons == [reason]


async def test_policy_unavailable_denies(engine, store, policy):
    policy.error = "policy_unavailable"
    outcome = await engine.decide(freeze())
    assert outcome.reasons == ["policy_unavailable"]
    assert store.audit[0].decision == "denied"


async def test_require_approval_creates_one_pending_approval(engine, store, policy):
    policy.decision = NEEDS_APPROVAL
    first = await engine.decide(freeze())
    second = await engine.decide(freeze())
    assert first.status == "pending_approval"
    assert first.approval_id == second.approval_id == "apr_1"
    assert first.expires_at is not None
    assert list(store.approvals) == ["apr_1"]
    assert [r.event_type for r in store.audit] == ["approval_requested", "decision"]


async def test_approved_call_is_redeemed_once(engine, store, policy, clock):
    policy.decision = NEEDS_APPROVAL
    await engine.decide(freeze())
    await store.transition(
        "apr_1", from_statuses=(ApprovalStatus.PENDING,), to=ApprovalStatus.APPROVED, at=clock()
    )
    redeemed = await engine.decide(freeze())
    again = await engine.decide(freeze())
    assert redeemed.status == "allowed"
    assert store.approvals["apr_1"].status is ApprovalStatus.USED
    assert (again.status, again.approval_id) == ("pending_approval", "apr_2")
    assert "approval_redeemed" in [r.event_type for r in store.audit]
    assert first_invalid(store.audit) is None


async def test_changed_arguments_need_new_approval(engine, store, policy, clock):
    policy.decision = NEEDS_APPROVAL
    await engine.decide(freeze(25000))
    await store.transition(
        "apr_1", from_statuses=(ApprovalStatus.PENDING,), to=ApprovalStatus.APPROVED, at=clock()
    )
    outcome = await engine.decide(freeze(26000))
    assert (outcome.status, outcome.approval_id) == ("pending_approval", "apr_2")
    assert store.approvals["apr_1"].status is ApprovalStatus.APPROVED


async def test_policy_deny_wins_over_existing_approval(engine, store, policy, clock):
    policy.decision = NEEDS_APPROVAL
    await engine.decide(freeze())
    await store.transition(
        "apr_1", from_statuses=(ApprovalStatus.PENDING,), to=ApprovalStatus.APPROVED, at=clock()
    )
    policy.decision = policy_result(Decision.DENY, "agent_suspended")
    outcome = await engine.decide(freeze())
    assert outcome.reasons == ["agent_suspended"]
    assert store.approvals["apr_1"].status is ApprovalStatus.APPROVED


async def test_expired_approval_is_replaced(engine, store, policy, clock):
    policy.decision = NEEDS_APPROVAL
    await engine.decide(freeze())
    clock.advance(hours=25)
    outcome = await engine.decide(freeze())
    assert outcome.approval_id == "apr_2"
    assert store.approvals["apr_1"].status is ApprovalStatus.EXPIRED
    assert "approval_expired" in [r.event_type for r in store.audit]


async def test_store_unavailable_denies(engine, store):
    store.fail = StoreUnavailable("down")
    outcome = await engine.decide(freeze(5000))
    assert outcome.reasons == ["governance_store_unavailable"]


async def test_audit_failure_denies(engine, store):
    store.fail = StoreError("insert failed")
    outcome = await engine.decide(freeze(5000))
    assert outcome.reasons == ["audit_write_failed"]
```

- [ ] **Step 2: 確認失敗**

Run: `uv run pytest tests/test_engine.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'ai_gov_gateway.engine.engine'`

- [ ] **Step 3: 實作 `src/ai_gov_gateway/engine/engine.py`**

```python
import asyncio
import secrets
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from ai_gov_gateway.engine.audit import approval_event
from ai_gov_gateway.engine.clock import Clock, utc_now
from ai_gov_gateway.engine.fingerprint import call_fingerprint, parse_agent_key
from ai_gov_gateway.engine.models import (
    AgentRef,
    Approval,
    ApprovalStatus,
    AuditEvent,
    AuditEventType,
    Decision,
    Outcome,
    PolicyDecision,
    ToolCall,
    UserIdentity,
)
from ai_gov_gateway.engine.policy import PolicyClient, PolicyError
from ai_gov_gateway.engine.store import GovernanceStore, StoreError, StoreUnavailable
from ai_gov_gateway.engine.tokens import TokenError, TokenVerifier

OPEN_STATUSES = (ApprovalStatus.PENDING, ApprovalStatus.APPROVED)


class _Denied(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def new_approval_id() -> str:
    return "apr_" + secrets.token_hex(8)


def policy_input(agent: AgentRef, user: UserIdentity, call: ToolCall) -> dict[str, Any]:
    return {
        "agent": agent.model_dump(),
        "user": user.model_dump(),
        "tool": {"server": call.server, "name": call.tool},
        "arguments": call.arguments,
    }


class GovernanceEngine:
    def __init__(
        self,
        *,
        verifier: TokenVerifier,
        policy: PolicyClient,
        store: GovernanceStore,
        approval_ttl: timedelta,
        approver_role: str,
        clock: Clock = utc_now,
        new_id: Callable[[], str] = new_approval_id,
    ) -> None:
        self._verifier = verifier
        self._policy = policy
        self._store = store
        self._ttl = approval_ttl
        self._approver_role = approver_role
        self._clock = clock
        self._new_id = new_id

    async def decide(self, call: ToolCall) -> Outcome:
        """一律 fail-closed：無法判斷或無法寫入稽核時拒絕。"""
        agent = parse_agent_key(call.key_alias)
        user: UserIdentity | None = None
        try:
            if agent is None:
                raise _Denied("unregistered_agent_version")
            user = await self._verify_user(call.user_token)
            decision = await self._policy.evaluate(policy_input(agent, user, call))
            if decision.decision is Decision.REQUIRE_APPROVAL:
                return await self._require_approval(agent, user, call, decision)
            outcome = (
                Outcome.allowed(decision.reasons)
                if decision.decision is Decision.ALLOW
                else Outcome.denied(decision.reasons)
            )
            await self._record(call, agent, user, outcome, decision.policy_version)
            return outcome
        except _Denied as denied:
            return await self._deny(call, agent, user, denied.code)
        except PolicyError as error:
            return await self._deny(call, agent, user, error.code)
        except StoreUnavailable:
            return Outcome.denied(["governance_store_unavailable"])
        except StoreError:
            return Outcome.denied(["audit_write_failed"])

    async def _verify_user(self, token: str | None) -> UserIdentity:
        if not token:
            raise _Denied("missing_user_token")
        try:
            return await asyncio.to_thread(self._verifier.verify, token)
        except TokenError as error:
            raise _Denied("invalid_user_token") from error

    async def _deny(
        self, call: ToolCall, agent: AgentRef | None, user: UserIdentity | None, code: str
    ) -> Outcome:
        outcome = Outcome.denied([code])
        try:
            await self._record(call, agent, user, outcome, None)
        except StoreUnavailable:
            return Outcome.denied([code, "governance_store_unavailable"])
        except StoreError:
            return Outcome.denied([code, "audit_write_failed"])
        return outcome

    async def _require_approval(
        self, agent: AgentRef, user: UserIdentity, call: ToolCall, decision: PolicyDecision
    ) -> Outcome:
        fingerprint = call_fingerprint(agent, user.sub, call.server, call.tool, call.arguments)
        now = self._clock()
        existing = await self._current_approval(fingerprint)
        if existing is not None and existing.status is ApprovalStatus.APPROVED:
            outcome = Outcome.allowed(decision.reasons)
            redeemed = self._event(
                "approval_redeemed", call, agent, user, outcome, decision.policy_version, existing.id
            )
            if await self._store.transition(
                existing.id,
                from_statuses=(ApprovalStatus.APPROVED,),
                to=ApprovalStatus.USED,
                at=now,
                audit=redeemed,
            ):
                return outcome
            # 同一個核准被另一個請求搶先使用；重新查詢後依現況處理
            existing = await self._current_approval(fingerprint)
        if existing is not None and existing.status is ApprovalStatus.PENDING:
            outcome = Outcome.pending(existing.id, existing.expires_at, decision.reasons)
            await self._record(call, agent, user, outcome, decision.policy_version, existing.id)
            return outcome
        approval = Approval(
            id=self._new_id(),
            fingerprint=fingerprint,
            status=ApprovalStatus.PENDING,
            agent=agent,
            user_sub=user.sub,
            username=user.username,
            server=call.server,
            tool=call.tool,
            arguments=call.arguments,
            reasons=decision.reasons,
            approver_role=decision.approver_role or self._approver_role,
            created_at=now,
            expires_at=now + self._ttl,
        )
        outcome = Outcome.pending(approval.id, approval.expires_at, decision.reasons)
        requested = self._event(
            "approval_requested", call, agent, user, outcome, decision.policy_version, approval.id
        )
        await self._store.create_approval(approval, requested)
        return outcome

    async def _current_approval(self, fingerprint: str) -> Approval | None:
        approval = await self._store.open_approval(fingerprint)
        now = self._clock()
        if approval is None or approval.expires_at > now:
            return approval
        await self._store.transition(
            approval.id,
            from_statuses=OPEN_STATUSES,
            to=ApprovalStatus.EXPIRED,
            at=now,
            audit=approval_event("approval_expired", approval, decision=ApprovalStatus.EXPIRED),
        )
        return None

    async def _record(
        self,
        call: ToolCall,
        agent: AgentRef | None,
        user: UserIdentity | None,
        outcome: Outcome,
        policy_version: str | None,
        approval_id: str | None = None,
    ) -> None:
        event = self._event("decision", call, agent, user, outcome, policy_version, approval_id)
        await self._store.append_audit(event, self._clock())

    @staticmethod
    def _event(
        event_type: AuditEventType,
        call: ToolCall,
        agent: AgentRef | None,
        user: UserIdentity | None,
        outcome: Outcome,
        policy_version: str | None,
        approval_id: str | None,
    ) -> AuditEvent:
        return AuditEvent(
            event_type=event_type,
            agent_id=agent.id if agent else None,
            agent_version=agent.version if agent else None,
            key_alias=call.key_alias,
            user_sub=user.sub if user else None,
            username=user.username if user else None,
            server=call.server,
            tool=call.tool,
            arguments=call.arguments,
            decision=outcome.status,
            reasons=outcome.reasons,
            approval_id=approval_id,
            policy_version=policy_version,
        )
```

- [ ] **Step 4: 確認通過並 Commit**

Run: `uv run pytest tests/test_engine.py -q && uv run ruff format src tests && uv run ruff check src tests && uv run mypy src`
Expected: 13 passed

```bash
git add src/ai_gov_gateway/engine/engine.py tests/test_engine.py
git commit -m "feat: add governance engine with approval redemption"
```

---

### Task 10: 審批服務

**Files:**
- Create: `src/ai_gov_gateway/engine/approvals.py`
- Test: `tests/test_approvals.py`

- [ ] **Step 1: 寫失敗的測試 `tests/test_approvals.py`**

```python
from datetime import timedelta

import pytest

from ai_gov_gateway.engine.approvals import ApprovalError, ApprovalService
from ai_gov_gateway.engine.engine import GovernanceEngine
from ai_gov_gateway.engine.models import ApprovalStatus, Decision, ToolCall, UserIdentity
from tests.fakes import FakeClock, FakePolicy, InMemoryStore, id_sequence, policy_result
from tests.jwt_helpers import alice_token, make_verifier

ALICE = UserIdentity(sub="alice-sub", username="alice", roles=["analyst", "compliance_supervisor"])
BOB = UserIdentity(sub="bob-sub", username="bob", roles=["compliance_supervisor"])
ANALYST = UserIdentity(sub="dave-sub", username="dave", roles=["analyst"])


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture
def service(store, clock) -> ApprovalService:
    return ApprovalService(store=store, approver_role="compliance_supervisor", clock=clock)


@pytest.fixture
def engine(store, clock) -> GovernanceEngine:
    return GovernanceEngine(
        verifier=make_verifier(),
        policy=FakePolicy(policy_result(Decision.REQUIRE_APPROVAL, "amount_over_threshold")),
        store=store,
        approval_ttl=timedelta(hours=24),
        approver_role="compliance_supervisor",
        clock=clock,
        new_id=id_sequence(),
    )


@pytest.fixture
def pending(engine):
    async def create() -> str:
        call = ToolCall(
            key_alias="kyt-agent@1.4",
            user_token=alice_token(),
            server="account_ops",
            tool="freeze_account",
            arguments={"account_id": "acct-001", "amount_usd": 25000, "counterparty": "0x1"},
        )
        outcome = await engine.decide(call)
        assert outcome.approval_id is not None
        return outcome.approval_id

    return create


async def test_other_supervisor_approves(service, store, pending):
    approval_id = await pending()
    approved = await service.approve(BOB, approval_id, "已確認")
    assert approved.status is ApprovalStatus.APPROVED
    assert (approved.decided_by_username, approved.comment) == ("bob", "已確認")
    event = store.audit[-1]
    assert (event.event_type, event.actor_username, event.comment) == ("approval_approved", "bob", "已確認")


async def test_delegator_cannot_approve_own_request(service, pending):
    approval_id = await pending()
    with pytest.raises(ApprovalError) as error:
        await service.approve(ALICE, approval_id, "")
    assert (error.value.status_code, error.value.code) == (403, "self_approval_forbidden")


async def test_delegator_may_reject_own_request(service, pending):
    approval_id = await pending()
    rejected = await service.reject(ALICE, approval_id, "撤回")
    assert rejected.status is ApprovalStatus.REJECTED


async def test_role_required(service, pending):
    approval_id = await pending()
    for action in (service.list(ANALYST, None), service.approve(ANALYST, approval_id, "")):
        with pytest.raises(ApprovalError) as error:
            await action
        assert (error.value.status_code, error.value.code) == (403, "role_required")


async def test_decided_approval_cannot_be_decided_again(service, pending):
    approval_id = await pending()
    await service.reject(BOB, approval_id, "")
    with pytest.raises(ApprovalError) as error:
        await service.approve(BOB, approval_id, "")
    assert (error.value.status_code, error.value.code) == (409, "approval_not_pending")


async def test_expired_approval_cannot_be_approved(service, store, clock, pending):
    approval_id = await pending()
    clock.advance(hours=25)
    with pytest.raises(ApprovalError) as error:
        await service.approve(BOB, approval_id, "")
    assert (error.value.status_code, error.value.code) == (409, "approval_expired")
    assert store.approvals[approval_id].status is ApprovalStatus.EXPIRED
    assert store.audit[-1].event_type == "approval_expired"


async def test_unknown_approval(service):
    with pytest.raises(ApprovalError) as error:
        await service.get(BOB, "apr_missing")
    assert (error.value.status_code, error.value.code) == (404, "approval_not_found")


async def test_detail_includes_history_and_list_filters(service, clock, pending):
    first = await pending()
    await service.reject(BOB, first, "金額有誤")
    clock.advance(minutes=1)
    second = await pending()
    detail = await service.get(BOB, second)
    assert [a.id for a in detail.history] == [first]
    assert [a.id for a in await service.list(BOB, ApprovalStatus.PENDING)] == [second]


async def test_verify_audit(service, store, pending):
    await pending()
    result = await service.verify_audit(BOB)
    assert (result.records, result.first_invalid_id) == (1, None)
    store.audit[0] = store.audit[0].model_copy(update={"username": "mallory"})
    assert (await service.verify_audit(BOB)).first_invalid_id == 1
```

- [ ] **Step 2: 確認失敗**

Run: `uv run pytest tests/test_approvals.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'ai_gov_gateway.engine.approvals'`

- [ ] **Step 3: 實作 `src/ai_gov_gateway/engine/approvals.py`**

```python
from pydantic import BaseModel

from ai_gov_gateway.engine.audit import approval_event, first_invalid
from ai_gov_gateway.engine.clock import Clock, utc_now
from ai_gov_gateway.engine.models import Approval, ApprovalStatus, UserIdentity
from ai_gov_gateway.engine.store import GovernanceStore

OPEN_STATUSES = (ApprovalStatus.PENDING, ApprovalStatus.APPROVED)


class ApprovalError(Exception):
    def __init__(self, status_code: int, code: str) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code


class ApprovalDetail(BaseModel):
    approval: Approval
    history: list[Approval]


class AuditVerification(BaseModel):
    records: int
    first_invalid_id: int | None


class ApprovalService:
    def __init__(
        self, *, store: GovernanceStore, approver_role: str, clock: Clock = utc_now
    ) -> None:
        self._store = store
        self._approver_role = approver_role
        self._clock = clock

    async def list(self, actor: UserIdentity, status: ApprovalStatus | None) -> list[Approval]:
        self._require_role(actor)
        refreshed = [await self._expire_if_due(a) for a in await self._store.list_approvals(status)]
        return [a for a in refreshed if status is None or a.status is status]

    async def get(self, actor: UserIdentity, approval_id: str) -> ApprovalDetail:
        self._require_role(actor)
        approval = await self._load(approval_id)
        history = [
            a for a in await self._store.approvals_for(approval.fingerprint) if a.id != approval.id
        ]
        return ApprovalDetail(approval=approval, history=history)

    async def approve(self, actor: UserIdentity, approval_id: str, comment: str) -> Approval:
        return await self._decide(actor, approval_id, comment, ApprovalStatus.APPROVED)

    async def reject(self, actor: UserIdentity, approval_id: str, comment: str) -> Approval:
        return await self._decide(actor, approval_id, comment, ApprovalStatus.REJECTED)

    async def verify_audit(self, actor: UserIdentity) -> AuditVerification:
        self._require_role(actor)
        records = await self._store.audit_records()
        return AuditVerification(records=len(records), first_invalid_id=first_invalid(records))

    async def _decide(
        self, actor: UserIdentity, approval_id: str, comment: str, to: ApprovalStatus
    ) -> Approval:
        self._require_role(actor)
        approval = await self._load(approval_id)
        if approval.status is ApprovalStatus.EXPIRED:
            raise ApprovalError(409, "approval_expired")
        if approval.status is not ApprovalStatus.PENDING:
            raise ApprovalError(409, "approval_not_pending")
        if to is ApprovalStatus.APPROVED and actor.sub == approval.user_sub:
            raise ApprovalError(403, "self_approval_forbidden")
        event_type = "approval_approved" if to is ApprovalStatus.APPROVED else "approval_rejected"
        changed = await self._store.transition(
            approval_id,
            from_statuses=(ApprovalStatus.PENDING,),
            to=to,
            at=self._clock(),
            actor=actor,
            comment=comment,
            audit=approval_event(event_type, approval, actor=actor, decision=to, comment=comment),
        )
        if not changed:
            raise ApprovalError(409, "approval_not_pending")
        return await self._load(approval_id)

    async def _load(self, approval_id: str) -> Approval:
        approval = await self._store.get_approval(approval_id)
        if approval is None:
            raise ApprovalError(404, "approval_not_found")
        return await self._expire_if_due(approval)

    async def _expire_if_due(self, approval: Approval) -> Approval:
        now = self._clock()
        if approval.status not in OPEN_STATUSES or approval.expires_at > now:
            return approval
        await self._store.transition(
            approval.id,
            from_statuses=OPEN_STATUSES,
            to=ApprovalStatus.EXPIRED,
            at=now,
            audit=approval_event("approval_expired", approval, decision=ApprovalStatus.EXPIRED),
        )
        refreshed = await self._store.get_approval(approval.id)
        return refreshed or approval

    def _require_role(self, actor: UserIdentity) -> None:
        if self._approver_role not in actor.roles:
            raise ApprovalError(403, "role_required")
```

`decision=to` 傳入 `ApprovalStatus`（`StrEnum`），`AuditEvent.decision` 為 `str`，pydantic 會原樣保存字串值。

- [ ] **Step 4: 確認通過並 Commit**

Run: `uv run pytest tests/test_approvals.py -q && uv run ruff format src tests && uv run ruff check src tests && uv run mypy src`
Expected: 9 passed

```bash
git add src/ai_gov_gateway/engine/approvals.py tests/test_approvals.py
git commit -m "feat: add approval service with maker-checker"
```

---

### Task 11: Postgres store

**Files:**
- Create: `src/ai_gov_gateway/engine/postgres.py`
- Modify: `docker-compose.yml`（postgres 開 `127.0.0.1:5433`）、`.env.example`
- Test: `tests/integration/__init__.py`（空檔）、`tests/integration/test_postgres_store.py`

- [ ] **Step 1: 開放 Postgres port 並設定測試用連線**

`docker-compose.yml` 的 `postgres` 服務新增：

```yaml
    ports:
      - "127.0.0.1:5433:5432"
```

`.env.example` 結尾新增：

```bash
# 主機上的整合測試使用（資料庫不存在時會自動建立）
GOV_TEST_DATABASE_URL=postgresql://litellm:<POSTGRES_PASSWORD>@127.0.0.1:5433/governance_test
```

並在 `.env` 加入實際值（不輸出內容）：

```bash
python3 - <<'EOF'
import re, pathlib
env = pathlib.Path(".env"); text = env.read_text()
pw = re.search(r"^POSTGRES_PASSWORD=(.*)$", text, re.M).group(1)
if "GOV_TEST_DATABASE_URL=" not in text:
    text += f"\nGOV_TEST_DATABASE_URL=postgresql://litellm:{pw}@127.0.0.1:5433/governance_test\n"
env.write_text(text); print("ok")
EOF
docker compose up -d postgres
```

Expected: `ok`，postgres 容器重建並開放 5433

- [ ] **Step 2: 寫失敗的整合測試 `tests/integration/test_postgres_store.py`**

```python
import os
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from dotenv import load_dotenv

from ai_gov_gateway.engine.audit import first_invalid
from ai_gov_gateway.engine.models import AgentRef, Approval, ApprovalStatus, AuditEvent, UserIdentity
from ai_gov_gateway.engine.postgres import PostgresStore
from ai_gov_gateway.engine.store import StoreUnavailable

pytestmark = pytest.mark.integration
load_dotenv()
URL = os.environ.get("GOV_TEST_DATABASE_URL", "")
NOW = datetime(2026, 10, 20, 9, tzinfo=UTC)
BOB = UserIdentity(sub="bob-sub", username="bob", roles=["compliance_supervisor"])


def _approval(approval_id: str, fingerprint: str = "fp") -> Approval:
    return Approval(
        id=approval_id,
        fingerprint=fingerprint,
        status=ApprovalStatus.PENDING,
        agent=AgentRef(id="kyt-agent", version="1.4", key_alias="kyt-agent@1.4"),
        user_sub="alice-sub",
        username="alice",
        server="account_ops",
        tool="freeze_account",
        arguments={"amount_usd": 25000, "account_id": "acct-001"},
        reasons=["amount_over_threshold"],
        approver_role="compliance_supervisor",
        created_at=NOW,
        expires_at=NOW + timedelta(hours=24),
    )


@pytest.fixture
async def store() -> PostgresStore:
    if not URL:
        pytest.skip("GOV_TEST_DATABASE_URL not set")
    store = PostgresStore(URL)
    await store.ensure_schema()
    async with await psycopg.AsyncConnection.connect(URL) as conn:
        await conn.execute("TRUNCATE approvals, audit_events")
    return store


async def test_ensure_schema_is_idempotent(store):
    await store.ensure_schema()


async def test_approval_round_trip_and_transition(store):
    await store.create_approval(_approval("apr_1"), AuditEvent(event_type="approval_requested"))
    assert (await store.open_approval("fp")).id == "apr_1"
    changed = await store.transition(
        "apr_1",
        from_statuses=(ApprovalStatus.PENDING,),
        to=ApprovalStatus.APPROVED,
        at=NOW + timedelta(minutes=5),
        actor=BOB,
        comment="ok",
        audit=AuditEvent(event_type="approval_approved", actor_sub="bob-sub"),
    )
    again = await store.transition(
        "apr_1", from_statuses=(ApprovalStatus.PENDING,), to=ApprovalStatus.REJECTED, at=NOW
    )
    approval = await store.get_approval("apr_1")
    assert (changed, again) == (True, False)
    assert (approval.status, approval.decided_by_username, approval.comment) == (
        ApprovalStatus.APPROVED,
        "bob",
        "ok",
    )
    assert approval.arguments == {"amount_usd": 25000, "account_id": "acct-001"}
    assert [a.id for a in await store.list_approvals(ApprovalStatus.APPROVED)] == ["apr_1"]


async def test_audit_chain_survives_round_trip(store):
    for n in range(3):
        await store.append_audit(
            AuditEvent(event_type="decision", arguments={"n": n, "amount_usd": 1.5}, reasons=["r"]),
            NOW + timedelta(seconds=n),
        )
    records = await store.audit_records()
    assert [r.id for r in records] == [1, 2, 3]
    assert first_invalid(records) is None


async def test_unreachable_database_raises_store_unavailable():
    store = PostgresStore("postgresql://nobody:x@127.0.0.1:1/none")
    with pytest.raises(StoreUnavailable):
        await store.audit_records()
```

- [ ] **Step 3: 確認失敗**

Run: `uv run pytest -m integration tests/integration/test_postgres_store.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'ai_gov_gateway.engine.postgres'`

- [ ] **Step 4: 實作 `src/ai_gov_gateway/engine/postgres.py`**

```python
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from enum import StrEnum
from typing import Any

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb

from ai_gov_gateway.engine.audit import GENESIS_HASH, build_record
from ai_gov_gateway.engine.models import (
    AgentRef,
    Approval,
    ApprovalStatus,
    AuditEvent,
    AuditRecord,
    UserIdentity,
)
from ai_gov_gateway.engine.store import StoreError, StoreUnavailable, transition_columns

# 固定的 advisory lock key，讓多個 worker 寫入稽核時 hash chain 維持單一序列
AUDIT_LOCK_KEY = 72_410_001

SCHEMA = """
CREATE TABLE IF NOT EXISTS approvals (
    id text PRIMARY KEY,
    fingerprint text NOT NULL,
    status text NOT NULL,
    agent_id text NOT NULL,
    agent_version text NOT NULL,
    key_alias text NOT NULL,
    user_sub text NOT NULL,
    username text NOT NULL,
    server text NOT NULL,
    tool text NOT NULL,
    arguments jsonb NOT NULL,
    reasons jsonb NOT NULL,
    approver_role text NOT NULL,
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    decided_by text,
    decided_by_username text,
    decided_at timestamptz,
    comment text,
    used_at timestamptz
);
CREATE INDEX IF NOT EXISTS approvals_fingerprint ON approvals (fingerprint, created_at);
CREATE TABLE IF NOT EXISTS audit_events (
    id bigint PRIMARY KEY,
    ts timestamptz NOT NULL,
    event_type text NOT NULL,
    agent_id text,
    agent_version text,
    key_alias text,
    user_sub text,
    username text,
    actor_sub text,
    actor_username text,
    server text,
    tool text,
    arguments jsonb,
    decision text,
    reasons jsonb NOT NULL,
    approval_id text,
    policy_version text,
    comment text,
    prev_hash text NOT NULL,
    hash text NOT NULL
);
"""

AUDIT_COLUMNS = (
    "id",
    "ts",
    "event_type",
    "agent_id",
    "agent_version",
    "key_alias",
    "user_sub",
    "username",
    "actor_sub",
    "actor_username",
    "server",
    "tool",
    "arguments",
    "decision",
    "reasons",
    "approval_id",
    "policy_version",
    "comment",
    "prev_hash",
    "hash",
)
APPROVAL_FIELDS = (
    "id",
    "fingerprint",
    "status",
    "user_sub",
    "username",
    "server",
    "tool",
    "arguments",
    "reasons",
    "approver_role",
    "created_at",
    "expires_at",
    "decided_by",
    "decided_by_username",
    "decided_at",
    "comment",
    "used_at",
)
JSON_COLUMNS = {"arguments", "reasons"}


def _db_value(column: str, value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if column in JSON_COLUMNS and value is not None:
        return Jsonb(value)
    return value


def _approval_from_row(row: DictRow) -> Approval:
    agent = AgentRef(id=row["agent_id"], version=row["agent_version"], key_alias=row["key_alias"])
    return Approval(agent=agent, **{field: row[field] for field in APPROVAL_FIELDS})


def _insert(table: str, columns: dict[str, Any]) -> tuple[sql.Composed, list[Any]]:
    query = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
        sql.Identifier(table),
        sql.SQL(", ").join(sql.Identifier(c) for c in columns),
        sql.SQL(", ").join(sql.Placeholder() for _ in columns),
    )
    return query, [_db_value(c, v) for c, v in columns.items()]


class PostgresStore:
    def __init__(self, database_url: str) -> None:
        self._url = database_url

    async def ensure_schema(self) -> None:
        await self._ensure_database()
        async with self._connect() as conn:
            await conn.execute(SCHEMA)

    async def _ensure_database(self) -> None:
        name = str(conninfo_to_dict(self._url)["dbname"])
        admin_url = make_conninfo(self._url, dbname="postgres")
        try:
            async with await psycopg.AsyncConnection.connect(admin_url, autocommit=True) as conn:
                cursor = await conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
                if await cursor.fetchone() is None:
                    await conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        except psycopg.OperationalError as error:
            raise StoreUnavailable(str(error)) from error

    @asynccontextmanager
    async def _connect(self) -> AsyncIterator[psycopg.AsyncConnection[DictRow]]:
        try:
            conn = await psycopg.AsyncConnection.connect(self._url, row_factory=dict_row)
        except psycopg.OperationalError as error:
            raise StoreUnavailable(str(error)) from error
        try:
            async with conn:
                yield conn
        except psycopg.OperationalError as error:
            raise StoreUnavailable(str(error)) from error
        except psycopg.Error as error:
            raise StoreError(str(error)) from error

    async def open_approval(self, fingerprint: str) -> Approval | None:
        async with self._connect() as conn:
            cursor = await conn.execute(
                "SELECT * FROM approvals WHERE fingerprint = %s AND status = ANY(%s) "
                "ORDER BY created_at DESC LIMIT 1",
                (fingerprint, [ApprovalStatus.PENDING.value, ApprovalStatus.APPROVED.value]),
            )
            row = await cursor.fetchone()
        return _approval_from_row(row) if row else None

    async def approvals_for(self, fingerprint: str) -> list[Approval]:
        async with self._connect() as conn:
            cursor = await conn.execute(
                "SELECT * FROM approvals WHERE fingerprint = %s ORDER BY created_at", (fingerprint,)
            )
            rows = await cursor.fetchall()
        return [_approval_from_row(row) for row in rows]

    async def get_approval(self, approval_id: str) -> Approval | None:
        async with self._connect() as conn:
            cursor = await conn.execute("SELECT * FROM approvals WHERE id = %s", (approval_id,))
            row = await cursor.fetchone()
        return _approval_from_row(row) if row else None

    async def list_approvals(self, status: ApprovalStatus | None) -> list[Approval]:
        async with self._connect() as conn:
            if status is None:
                cursor = await conn.execute("SELECT * FROM approvals ORDER BY created_at DESC")
            else:
                cursor = await conn.execute(
                    "SELECT * FROM approvals WHERE status = %s ORDER BY created_at DESC",
                    (status.value,),
                )
            rows = await cursor.fetchall()
        return [_approval_from_row(row) for row in rows]

    async def create_approval(self, approval: Approval, audit: AuditEvent) -> AuditRecord:
        columns = approval.model_dump(exclude={"agent"}) | {
            "agent_id": approval.agent.id,
            "agent_version": approval.agent.version,
            "key_alias": approval.agent.key_alias,
        }
        async with self._connect() as conn:
            query, params = _insert("approvals", columns)
            await conn.execute(query, params)
            return await self._append(conn, audit, approval.created_at)

    async def transition(
        self,
        approval_id: str,
        *,
        from_statuses: tuple[ApprovalStatus, ...],
        to: ApprovalStatus,
        at: datetime,
        actor: UserIdentity | None = None,
        comment: str | None = None,
        audit: AuditEvent | None = None,
    ) -> bool:
        columns = transition_columns(to, at, actor, comment)
        query = sql.SQL("UPDATE approvals SET {} WHERE id = %s AND status = ANY(%s)").format(
            sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(c)) for c in columns)
        )
        params = [_db_value(c, v) for c, v in columns.items()]
        params += [approval_id, [s.value for s in from_statuses]]
        async with self._connect() as conn:
            cursor = await conn.execute(query, params)
            if cursor.rowcount != 1:
                return False
            if audit is not None:
                await self._append(conn, audit, at)
        return True

    async def append_audit(self, event: AuditEvent, ts: datetime) -> AuditRecord:
        async with self._connect() as conn:
            return await self._append(conn, event, ts)

    async def audit_records(self) -> list[AuditRecord]:
        async with self._connect() as conn:
            cursor = await conn.execute("SELECT * FROM audit_events ORDER BY id")
            rows = await cursor.fetchall()
        return [AuditRecord.model_validate(dict(row)) for row in rows]

    async def _append(
        self, conn: psycopg.AsyncConnection[DictRow], event: AuditEvent, ts: datetime
    ) -> AuditRecord:
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (AUDIT_LOCK_KEY,))
        cursor = await conn.execute("SELECT id, hash FROM audit_events ORDER BY id DESC LIMIT 1")
        last = await cursor.fetchone()
        record = build_record(
            record_id=(last["id"] + 1) if last else 1,
            prev_hash=last["hash"] if last else GENESIS_HASH,
            ts=ts,
            event=event,
        )
        query, params = _insert("audit_events", record.model_dump(include=set(AUDIT_COLUMNS)))
        await conn.execute(query, params)
        return record
```

`async with conn:` 在區塊正常結束時 commit、發生例外時 rollback，所以 `transition` 與 `create_approval` 的狀態變更和稽核在同一個 transaction。

- [ ] **Step 5: 確認整合測試通過並 Commit**

Run: `uv run pytest -m integration tests/integration/test_postgres_store.py -q && uv run pytest -q && uv run ruff format src tests && uv run ruff check src tests && uv run mypy src`
Expected: 4 passed（integration）；單元測試全部通過

```bash
git add src/ai_gov_gateway/engine/postgres.py tests/integration docker-compose.yml .env.example
git commit -m "feat: add Postgres governance store with atomic audit writes"
```

---

### Task 12: hook 轉接、執行期組裝與 LiteLLM plugin

**Files:**
- Create: `src/ai_gov_gateway/hook_adapter.py`、`src/ai_gov_gateway/runtime.py`、`src/ai_gov_gateway/plugin.py`
- Test: `tests/test_hook_adapter.py`

- [ ] **Step 1: 寫失敗的測試 `tests/test_hook_adapter.py`**

```python
from ai_gov_gateway.hook_adapter import tool_call_from_hook

# LiteLLM v1.105.1 call_mcp_tool 的 data 欄位（M1 V7 實際觀察）
M1_DATA = {
    "messages": [],
    "model": "x",
    "mcp_tool_name": "freeze_account",
    "mcp_arguments": {"account_id": "acct-001", "amount_usd": 25000},
    "mcp_server_name": "account_ops",
    "incoming_bearer_token": "eyJ.token.sig",
    "metadata": {},
}


def test_maps_litellm_fields():
    call = tool_call_from_hook(M1_DATA, "kyt-agent@1.4")
    assert call.key_alias == "kyt-agent@1.4"
    assert (call.server, call.tool) == ("account_ops", "freeze_account")
    assert call.arguments == {"account_id": "acct-001", "amount_usd": 25000}
    assert call.user_token == "eyJ.token.sig"


def test_strips_bearer_prefix():
    data = M1_DATA | {"incoming_bearer_token": "Bearer eyJ.token.sig"}
    assert tool_call_from_hook(data, None).user_token == "eyJ.token.sig"


def test_missing_fields_become_empty_and_fail_closed_downstream():
    call = tool_call_from_hook({}, None)
    assert (call.server, call.tool, call.arguments, call.user_token) == ("", "", {}, None)
```

- [ ] **Step 2: 確認失敗**

Run: `uv run pytest tests/test_hook_adapter.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'ai_gov_gateway.hook_adapter'`

- [ ] **Step 3: 實作 `hook_adapter.py`、`runtime.py`、`plugin.py`**

`src/ai_gov_gateway/hook_adapter.py`：

```python
from collections.abc import Mapping
from typing import Any

from ai_gov_gateway.engine.models import ToolCall


def _bearer(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    token = value.strip()
    return token[7:].strip() if token.lower().startswith("bearer ") else token


def tool_call_from_hook(data: Mapping[str, Any], key_alias: str | None) -> ToolCall:
    """LiteLLM v1.105.1 pre_mcp_call 的欄位；缺漏時留空，由 policy 判為 unknown_tool。"""
    arguments = data.get("mcp_arguments")
    return ToolCall(
        key_alias=key_alias,
        user_token=_bearer(data.get("incoming_bearer_token")),
        server=str(data.get("mcp_server_name") or ""),
        tool=str(data.get("mcp_tool_name") or ""),
        arguments=dict(arguments) if isinstance(arguments, Mapping) else {},
    )
```

`src/ai_gov_gateway/runtime.py`：

```python
import asyncio
from dataclasses import dataclass
from datetime import timedelta

from ai_gov_gateway.config import Settings
from ai_gov_gateway.engine.approvals import ApprovalService
from ai_gov_gateway.engine.engine import GovernanceEngine
from ai_gov_gateway.engine.policy import OpaPolicyClient
from ai_gov_gateway.engine.postgres import PostgresStore
from ai_gov_gateway.engine.tokens import TokenVerifier, jwks_resolver


@dataclass(frozen=True)
class Services:
    verifier: TokenVerifier
    engine: GovernanceEngine
    approvals: ApprovalService


_services: Services | None = None
_lock = asyncio.Lock()


async def build_services(settings: Settings) -> Services:
    store = PostgresStore(settings.database_url)
    await store.ensure_schema()
    verifier = TokenVerifier(
        issuer=settings.oidc_issuer,
        audience=settings.oidc_audience,
        key_resolver=jwks_resolver(settings.oidc_jwks_url),
    )
    engine = GovernanceEngine(
        verifier=verifier,
        policy=OpaPolicyClient(settings.opa_url, settings.opa_timeout_seconds),
        store=store,
        approval_ttl=timedelta(hours=settings.approval_ttl_hours),
        approver_role=settings.approver_role,
    )
    approvals = ApprovalService(store=store, approver_role=settings.approver_role)
    return Services(verifier=verifier, engine=engine, approvals=approvals)


async def get_services() -> Services:
    """第一次使用時才連線建立；失敗時下次呼叫會重試。"""
    global _services
    if _services is None:
        async with _lock:
            if _services is None:
                _services = await build_services(Settings())
    return _services
```

`src/ai_gov_gateway/plugin.py`：

```python
"""LiteLLM 依 guardrail 設定載入此模組；載入時把治理路由掛到同一個 app。

只在 gateway 映像內使用（需要 LiteLLM），主機上的單元測試不 import。
"""

import logging
from typing import Any

from litellm.integrations.custom_guardrail import CustomGuardrail
from litellm.proxy.proxy_server import app

from ai_gov_gateway.api.approvals import router
from ai_gov_gateway.engine.models import Outcome
from ai_gov_gateway.hook_adapter import tool_call_from_hook
from ai_gov_gateway.runtime import get_services

logger = logging.getLogger(__name__)

if not getattr(app.state, "governance_routes", False):
    app.include_router(router)
    app.state.governance_routes = True


class GovernanceBlocked(ValueError):
    """訊息為 JSON 字串，會原樣出現在 client 的 tool result（isError=True）。"""


class GovernanceGuardrail(CustomGuardrail):
    async def async_pre_call_hook(
        self, user_api_key_dict: Any, cache: Any, data: dict[str, Any], call_type: str
    ) -> dict[str, Any]:
        if call_type != "call_mcp_tool":
            return data
        try:
            call = tool_call_from_hook(data, getattr(user_api_key_dict, "key_alias", None))
            outcome = await (await get_services()).engine.decide(call)
        except Exception:
            logger.exception("governance decision failed")
            outcome = Outcome.denied(["governance_internal_error"])
        if outcome.status == "allowed":
            return data
        raise GovernanceBlocked(outcome.to_message())
```

`plugin.py` 依賴 Task 13 的 `api/approvals.py`；本 task 只確認 `hook_adapter` 與 `runtime`。

- [ ] **Step 4: 確認通過並 Commit**

Run: `uv run pytest tests/test_hook_adapter.py -q && uv run python -c "import ai_gov_gateway.runtime" && uv run ruff format src tests && uv run ruff check src tests`
Expected: 3 passed；匯入無錯誤

（`mypy src` 會因為 `plugin.py` 匯入尚未建立的 `ai_gov_gateway.api.approvals` 而失敗，留到 Task 13 一起檢查。）

```bash
git add src/ai_gov_gateway/hook_adapter.py src/ai_gov_gateway/runtime.py src/ai_gov_gateway/plugin.py tests/test_hook_adapter.py
git commit -m "feat: add LiteLLM governance plugin and runtime wiring"
```

---

### Task 13: 審批 API

**Files:**
- Create: `src/ai_gov_gateway/api/approvals.py`
- Test: `tests/test_api.py`

- [ ] **Step 1: 寫失敗的測試 `tests/test_api.py`**

```python
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_gov_gateway.api.approvals import router
from ai_gov_gateway.engine.approvals import ApprovalService
from ai_gov_gateway.engine.engine import GovernanceEngine
from ai_gov_gateway.engine.models import Decision, ToolCall
from ai_gov_gateway.runtime import Services, get_services
from tests.fakes import FakeClock, FakePolicy, InMemoryStore, id_sequence, policy_result
from tests.jwt_helpers import alice_token, bob_token, carol_token, make_verifier


@pytest.fixture
def services() -> Services:
    clock, store, verifier = FakeClock(), InMemoryStore(), make_verifier()
    policy = FakePolicy(policy_result(Decision.REQUIRE_APPROVAL, "amount_over_threshold"))
    engine = GovernanceEngine(
        verifier=verifier,
        policy=policy,
        store=store,
        approval_ttl=timedelta(hours=24),
        approver_role="compliance_supervisor",
        clock=clock,
        new_id=id_sequence(),
    )
    approvals = ApprovalService(store=store, approver_role="compliance_supervisor", clock=clock)
    return Services(verifier=verifier, engine=engine, approvals=approvals)


@pytest.fixture
def client(services) -> TestClient:
    app = FastAPI()
    app.include_router(router)

    async def override() -> Services:
        return services

    app.dependency_overrides[get_services] = override
    return TestClient(app)


async def _pending(services: Services) -> str:
    call = ToolCall(
        key_alias="kyt-agent@1.4",
        user_token=alice_token(),
        server="account_ops",
        tool="freeze_account",
        arguments={"account_id": "acct-001", "amount_usd": 25000, "counterparty": "0x1"},
    )
    outcome = await services.engine.decide(call)
    return outcome.approval_id


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_requires_token(client):
    assert client.get("/governance/approvals").status_code == 401
    response = client.get("/governance/approvals", headers=_auth("garbage"))
    assert (response.status_code, response.json()["detail"]) == (401, "invalid_token")


async def test_analyst_cannot_list(client, services):
    await _pending(services)
    response = client.get("/governance/approvals", headers=_auth(alice_token()))
    assert (response.status_code, response.json()["detail"]) == (403, "role_required")


async def test_supervisor_lists_and_approves(client, services):
    approval_id = await _pending(services)
    listed = client.get("/governance/approvals?status=pending", headers=_auth(bob_token()))
    assert [a["id"] for a in listed.json()] == [approval_id]
    detail = client.get(f"/governance/approvals/{approval_id}", headers=_auth(bob_token()))
    assert detail.json()["approval"]["arguments"]["amount_usd"] == 25000
    approved = client.post(
        f"/governance/approvals/{approval_id}/approve",
        json={"comment": "已確認"},
        headers=_auth(bob_token()),
    )
    assert (approved.status_code, approved.json()["status"]) == (200, "approved")
    again = client.post(
        f"/governance/approvals/{approval_id}/reject", json={}, headers=_auth(carol_token())
    )
    assert (again.status_code, again.json()["detail"]) == (409, "approval_not_pending")


async def test_not_found(client):
    response = client.get("/governance/approvals/apr_missing", headers=_auth(bob_token()))
    assert (response.status_code, response.json()["detail"]) == (404, "approval_not_found")


async def test_audit_verify(client, services):
    await _pending(services)
    response = client.get("/governance/audit/verify", headers=_auth(bob_token()))
    assert response.json() == {"records": 1, "first_invalid_id": None}
```

自己核准的 403 已由 `tests/test_approvals.py` 涵蓋；此處只測 HTTP 對應。

- [ ] **Step 2: 確認失敗**

Run: `uv run pytest tests/test_api.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'ai_gov_gateway.api.approvals'`

- [ ] **Step 3: 實作 `src/ai_gov_gateway/api/approvals.py`**

```python
import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from ai_gov_gateway.engine.approvals import ApprovalDetail, ApprovalError, AuditVerification
from ai_gov_gateway.engine.models import Approval, ApprovalStatus, UserIdentity
from ai_gov_gateway.engine.store import StoreError, StoreUnavailable
from ai_gov_gateway.engine.tokens import TokenError
from ai_gov_gateway.runtime import Services, get_services

router = APIRouter(prefix="/governance", tags=["governance"])
bearer = HTTPBearer(auto_error=False)

ServicesDep = Annotated[Services, Depends(get_services)]


class ReviewBody(BaseModel):
    comment: str = ""


async def current_user(
    services: ServicesDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> UserIdentity:
    # 審批以 IdP 的使用者身份驗證，不使用 LiteLLM virtual key：maker-checker 需要知道核准者是誰
    if credentials is None:
        raise HTTPException(status_code=401, detail="missing_token")
    try:
        return await asyncio.to_thread(services.verifier.verify, credentials.credentials)
    except TokenError as error:
        raise HTTPException(status_code=401, detail="invalid_token") from error


UserDep = Annotated[UserIdentity, Depends(current_user)]


def _http_error(error: Exception) -> HTTPException:
    if isinstance(error, ApprovalError):
        return HTTPException(status_code=error.status_code, detail=error.code)
    if isinstance(error, StoreUnavailable):
        return HTTPException(status_code=503, detail="governance_store_unavailable")
    return HTTPException(status_code=500, detail="audit_write_failed")


FAILURES = (ApprovalError, StoreUnavailable, StoreError)


@router.get("/approvals", response_model=list[Approval])
async def list_approvals(
    user: UserDep, services: ServicesDep, status: ApprovalStatus | None = None
) -> list[Approval]:
    try:
        return await services.approvals.list(user, status)
    except FAILURES as error:
        raise _http_error(error) from error


@router.get("/approvals/{approval_id}", response_model=ApprovalDetail)
async def get_approval(approval_id: str, user: UserDep, services: ServicesDep) -> ApprovalDetail:
    try:
        return await services.approvals.get(user, approval_id)
    except FAILURES as error:
        raise _http_error(error) from error


@router.post("/approvals/{approval_id}/approve", response_model=Approval)
async def approve(
    approval_id: str, body: ReviewBody, user: UserDep, services: ServicesDep
) -> Approval:
    try:
        return await services.approvals.approve(user, approval_id, body.comment)
    except FAILURES as error:
        raise _http_error(error) from error


@router.post("/approvals/{approval_id}/reject", response_model=Approval)
async def reject(
    approval_id: str, body: ReviewBody, user: UserDep, services: ServicesDep
) -> Approval:
    try:
        return await services.approvals.reject(user, approval_id, body.comment)
    except FAILURES as error:
        raise _http_error(error) from error


@router.get("/audit/verify", response_model=AuditVerification)
async def verify_audit(user: UserDep, services: ServicesDep) -> AuditVerification:
    try:
        return await services.approvals.verify_audit(user)
    except FAILURES as error:
        raise _http_error(error) from error
```

- [ ] **Step 4: 確認通過並 Commit**

Run: `uv run pytest -q && uv run ruff format src tests && uv run ruff check src tests && uv run mypy src`
Expected: 全部通過；mypy 無錯誤（`plugin.py` 的 litellm 匯入由 override 忽略）

```bash
git add src/ai_gov_gateway/api/approvals.py tests/test_api.py
git commit -m "feat: add governance approvals API"
```

---

### Task 14: 審批 CLI（`agw`）

**Files:**
- Create: `src/ai_gov_gateway/cli_auth.py`、`src/ai_gov_gateway/cli.py`
- Test: `tests/test_cli_auth.py`、`tests/test_cli.py`

- [ ] **Step 1: 寫失敗的測試 `tests/test_cli_auth.py`**

```python
import json
import stat

import httpx
import pytest

from ai_gov_gateway.cli_auth import (
    DeviceCode,
    LoginError,
    load_token,
    poll_token,
    save_token,
    start_device_flow,
)

ISSUER = "http://localhost:8080/realms/governance"
CODE = DeviceCode(
    device_code="dev", user_code="ABCD-EFGH", verification_uri="http://kc/device", expires_in=600
)


def _http(responses: list[httpx.Response], seen: list[httpx.Request] | None = None) -> httpx.Client:
    queue = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return next(queue)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_start_device_flow():
    seen: list[httpx.Request] = []
    body = {
        "device_code": "dev",
        "user_code": "ABCD-EFGH",
        "verification_uri": "http://kc/device",
        "verification_uri_complete": "http://kc/device?user_code=ABCD-EFGH",
        "expires_in": 600,
        "interval": 5,
    }
    code = start_device_flow(_http([httpx.Response(200, json=body)], seen), ISSUER, "agw-cli")
    assert code.user_code == "ABCD-EFGH"
    assert seen[0].url.path.endswith("/protocol/openid-connect/auth/device")


def test_poll_waits_until_authorized():
    sleeps: list[float] = []
    http = _http(
        [
            httpx.Response(400, json={"error": "authorization_pending"}),
            httpx.Response(400, json={"error": "slow_down"}),
            httpx.Response(200, json={"access_token": "tok"}),
        ]
    )
    assert poll_token(http, ISSUER, "agw-cli", CODE, sleep=sleeps.append) == "tok"
    assert sleeps == [5, 10]


def test_poll_stops_on_denied():
    http = _http([httpx.Response(400, json={"error": "access_denied"})])
    with pytest.raises(LoginError) as error:
        poll_token(http, ISSUER, "agw-cli", CODE, sleep=lambda s: None)
    assert error.value.code == "access_denied"


def test_token_file_is_private(tmp_path):
    path = tmp_path / "nested" / "token.json"
    save_token(path, "tok")
    assert load_token(path) == "tok"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text()) == {"access_token": "tok"}


def test_missing_token_file(tmp_path):
    with pytest.raises(LoginError) as error:
        load_token(tmp_path / "none.json")
    assert error.value.code == "not_logged_in"
```

- [ ] **Step 2: 寫失敗的測試 `tests/test_cli.py`**

```python
import json

import httpx
import pytest
from typer.testing import CliRunner

from ai_gov_gateway import cli
from ai_gov_gateway.cli_auth import save_token

runner = CliRunner()
APPROVAL = {
    "id": "apr_1",
    "status": "pending",
    "server": "account_ops",
    "tool": "freeze_account",
    "username": "alice",
    "reasons": ["amount_over_threshold"],
    "expires_at": "2026-10-21T09:00:00Z",
}


@pytest.fixture
def gateway(monkeypatch, tmp_path):
    token_path = tmp_path / "token.json"
    monkeypatch.setenv("AGW_TOKEN_PATH", str(token_path))
    save_token(token_path, "bob-token")
    seen: list[httpx.Request] = []
    responses: dict[tuple[str, str], httpx.Response] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return responses.get((request.method, request.url.path), httpx.Response(404, json={}))

    monkeypatch.setattr(
        cli, "make_client", lambda: httpx.Client(transport=httpx.MockTransport(handler))
    )
    return seen, responses


def test_list_pending(gateway):
    seen, responses = gateway
    responses[("GET", "/governance/approvals")] = httpx.Response(200, json=[APPROVAL])
    result = runner.invoke(cli.app, ["approvals", "list"])
    assert result.exit_code == 0, result.output
    assert "apr_1" in result.output and "account_ops.freeze_account" in result.output
    assert seen[0].url.params["status"] == "pending"
    assert seen[0].headers["Authorization"] == "Bearer bob-token"


def test_approve_sends_comment(gateway):
    seen, responses = gateway
    responses[("POST", "/governance/approvals/apr_1/approve")] = httpx.Response(
        200, json=APPROVAL | {"status": "approved"}
    )
    result = runner.invoke(cli.app, ["approvals", "approve", "apr_1", "--comment", "ok"])
    assert result.exit_code == 0, result.output
    assert json.loads(seen[0].read()) == {"comment": "ok"}
    assert "approved" in result.output


def test_error_detail_is_shown(gateway):
    _, responses = gateway
    responses[("POST", "/governance/approvals/apr_1/approve")] = httpx.Response(
        403, json={"detail": "self_approval_forbidden"}
    )
    result = runner.invoke(cli.app, ["approvals", "approve", "apr_1"])
    assert result.exit_code == 1
    assert "self_approval_forbidden" in result.output


def test_expired_token_asks_for_login(gateway):
    _, responses = gateway
    responses[("GET", "/governance/approvals")] = httpx.Response(401, json={"detail": "invalid_token"})
    result = runner.invoke(cli.app, ["approvals", "list"])
    assert result.exit_code == 1
    assert "agw login" in result.output


def test_audit_verify_fails_on_tampering(gateway):
    _, responses = gateway
    responses[("GET", "/governance/audit/verify")] = httpx.Response(
        200, json={"records": 5, "first_invalid_id": 3}
    )
    result = runner.invoke(cli.app, ["audit", "verify"])
    assert result.exit_code == 1
    assert "3" in result.output
```

- [ ] **Step 3: 確認失敗**

Run: `uv run pytest tests/test_cli_auth.py tests/test_cli.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'ai_gov_gateway.cli_auth'`

- [ ] **Step 4: 實作 `src/ai_gov_gateway/cli_auth.py`**

```python
import json
import time
from collections.abc import Callable
from pathlib import Path

import httpx
from pydantic import BaseModel

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


class LoginError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class DeviceCode(BaseModel):
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str | None = None
    expires_in: int
    interval: int = 5


def _error_code(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and body.get("error"):
        return str(body["error"])
    return f"http_{response.status_code}"


def start_device_flow(http: httpx.Client, issuer: str, client_id: str) -> DeviceCode:
    response = http.post(
        f"{issuer}/protocol/openid-connect/auth/device",
        data={"client_id": client_id, "scope": "openid"},
    )
    if response.status_code != 200:
        raise LoginError(_error_code(response))
    return DeviceCode.model_validate(response.json())


def poll_token(
    http: httpx.Client,
    issuer: str,
    client_id: str,
    code: DeviceCode,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """依 RFC 8628 輪詢；Keycloak 在逾時後回 expired_token，所以迴圈一定會結束。"""
    interval = code.interval
    while True:
        response = http.post(
            f"{issuer}/protocol/openid-connect/token",
            data={"grant_type": DEVICE_GRANT, "device_code": code.device_code, "client_id": client_id},
        )
        if response.status_code == 200:
            return str(response.json()["access_token"])
        error = _error_code(response)
        if error == "slow_down":
            interval += 5
        elif error != "authorization_pending":
            raise LoginError(error)
        sleep(interval)


def save_token(path: Path, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text(json.dumps({"access_token": token}))


def load_token(path: Path) -> str:
    try:
        return str(json.loads(path.read_text())["access_token"])
    except (FileNotFoundError, KeyError, ValueError) as error:
        raise LoginError("not_logged_in") from error
```

- [ ] **Step 5: 實作 `src/ai_gov_gateway/cli.py`**

```python
import json
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer
from pydantic_settings import BaseSettings, SettingsConfigDict

from ai_gov_gateway.cli_auth import LoginError, load_token, poll_token, save_token, start_device_flow


class CliSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGW_", extra="ignore")

    gateway_url: str = "http://localhost:4000"
    oidc_issuer: str = "http://localhost:8080/realms/governance"
    client_id: str = "agw-cli"
    token_path: Path = Path.home() / ".config" / "ai-gov-gateway" / "token.json"


app = typer.Typer(no_args_is_help=True, help="AI Governance Gateway 的審批與稽核指令")
approvals_app = typer.Typer(no_args_is_help=True, help="審批")
audit_app = typer.Typer(no_args_is_help=True, help="稽核")
app.add_typer(approvals_app, name="approvals")
app.add_typer(audit_app, name="audit")


def make_client() -> httpx.Client:
    return httpx.Client(timeout=10)


def _fail(message: str) -> typer.Exit:
    typer.echo(message, err=True)
    return typer.Exit(1)


@app.command()
def login() -> None:
    """以 Keycloak device flow 登入。"""
    settings = CliSettings()
    with make_client() as http:
        try:
            code = start_device_flow(http, settings.oidc_issuer, settings.client_id)
            typer.echo(f"請在瀏覽器開啟：{code.verification_uri_complete or code.verification_uri}")
            typer.echo(f"驗證碼：{code.user_code}")
            token = poll_token(http, settings.oidc_issuer, settings.client_id, code)
        except LoginError as error:
            raise _fail(f"登入失敗：{error.code}") from error
    save_token(settings.token_path, token)
    typer.echo("已登入")


def _request(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    params: dict[str, str] | None = None,
) -> Any:
    settings = CliSettings()
    try:
        token = load_token(settings.token_path)
    except LoginError as error:
        raise _fail("尚未登入，請先執行 agw login") from error
    with make_client() as http:
        response = http.request(
            method,
            f"{settings.gateway_url}/governance{path}",
            headers={"Authorization": f"Bearer {token}"},
            json=body,
            params=params,
        )
    if response.status_code == 401:
        raise _fail("token 無效或已過期，請重新執行 agw login")
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text
        raise _fail(f"失敗（{response.status_code}）：{detail}")
    return response.json()


def _line(row: dict[str, Any]) -> str:
    return (
        f"{row['id']}  {row['status']:<8}  {row['server']}.{row['tool']}  "
        f"委託人 {row['username']}  原因 {','.join(row['reasons'])}  到期 {row['expires_at']}"
    )


@approvals_app.command("list")
def list_approvals(
    status: Annotated[str, typer.Option(help="pending、approved、rejected、expired、used")] = "pending",
    show_all: Annotated[bool, typer.Option("--all", help="列出所有狀態")] = False,
) -> None:
    rows = _request("GET", "/approvals", params=None if show_all else {"status": status})
    if not rows:
        typer.echo("沒有符合的審批")
    for row in rows:
        typer.echo(_line(row))


@approvals_app.command("show")
def show(approval_id: str) -> None:
    typer.echo(json.dumps(_request("GET", f"/approvals/{approval_id}"), ensure_ascii=False, indent=2))


@approvals_app.command("approve")
def approve(approval_id: str, comment: Annotated[str, typer.Option()] = "") -> None:
    row = _request("POST", f"/approvals/{approval_id}/approve", body={"comment": comment})
    typer.echo(_line(row))


@approvals_app.command("reject")
def reject(approval_id: str, comment: Annotated[str, typer.Option()] = "") -> None:
    row = _request("POST", f"/approvals/{approval_id}/reject", body={"comment": comment})
    typer.echo(_line(row))


@audit_app.command("verify")
def verify() -> None:
    result = _request("GET", "/audit/verify")
    if result["first_invalid_id"] is not None:
        raise _fail(f"稽核紀錄被竄改：第一筆不一致的 id 為 {result['first_invalid_id']}")
    typer.echo(f"稽核紀錄完整（{result['records']} 筆）")
```

- [ ] **Step 6: 確認通過並 Commit**

Run: `uv run pytest -q && uv run ruff format src tests && uv run ruff check src tests && uv run mypy src && uv run agw --help`
Expected: 全部通過；`agw --help` 列出 `login`、`approvals`、`audit`

```bash
git add src/ai_gov_gateway/cli_auth.py src/ai_gov_gateway/cli.py tests/test_cli_auth.py tests/test_cli.py
git commit -m "feat: add agw CLI for approvals and audit verification"
```

---

### Task 15: 模擬下游 account_ops

**Files:**
- Create: `account_ops/__init__.py`（空檔）、`account_ops/state.py`、`account_ops/server.py`、`account_ops/Dockerfile`
- Create: `.dockerignore`
- Test: `tests/test_account_ops.py`

- [ ] **Step 1: 寫失敗的測試 `tests/test_account_ops.py`**

```python
from account_ops.state import AccountOps


def test_operations_update_state():
    ops = AccountOps()
    assert ops.add_to_watchlist("acct-1", "mixer exposure") == {
        "account_id": "acct-1",
        "status": "watchlisted",
    }
    frozen = ops.freeze_account("acct-1", 25000, "0x1", "sanctions")
    assert frozen == {"account_id": "acct-1", "status": "frozen", "amount_usd": 25000}
    assert ops.file_str("acct-1", "summary") == {"report_id": "str-0001", "status": "filed"}
    assert ops.file_str("acct-2", "summary")["report_id"] == "str-0002"
    assert ops.watchlist == {"acct-1": "mixer exposure"}
```

- [ ] **Step 2: 確認失敗**

Run: `uv run pytest tests/test_account_ops.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'account_ops'`

- [ ] **Step 3: 實作**

`account_ops/state.py`：

```python
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AccountOps:
    """模擬的帳戶操作；狀態只在記憶體中，重啟即清空。"""

    watchlist: dict[str, str] = field(default_factory=dict)
    frozen: dict[str, dict[str, Any]] = field(default_factory=dict)
    reports: list[dict[str, str]] = field(default_factory=list)

    def add_to_watchlist(self, account_id: str, reason: str) -> dict[str, Any]:
        self.watchlist[account_id] = reason
        return {"account_id": account_id, "status": "watchlisted"}

    def freeze_account(
        self, account_id: str, amount_usd: float, counterparty: str, reason: str
    ) -> dict[str, Any]:
        self.frozen[account_id] = {
            "amount_usd": amount_usd,
            "counterparty": counterparty,
            "reason": reason,
        }
        return {"account_id": account_id, "status": "frozen", "amount_usd": amount_usd}

    def file_str(self, account_id: str, summary: str) -> dict[str, Any]:
        report_id = f"str-{len(self.reports) + 1:04d}"
        self.reports.append({"report_id": report_id, "account_id": account_id, "summary": summary})
        return {"report_id": report_id, "status": "filed"}
```

`account_ops/server.py`：

```python
from typing import Any

from mcp.server.fastmcp import FastMCP

from account_ops.state import AccountOps

mcp = FastMCP("account_ops", host="0.0.0.0", port=8200, stateless_http=True)
ops = AccountOps()


@mcp.tool(description="Add an account to the monitoring watchlist.")
def add_to_watchlist(account_id: str, reason: str) -> dict[str, Any]:
    return ops.add_to_watchlist(account_id, reason)


@mcp.tool(description="Freeze an account. amount_usd is the balance to freeze.")
def freeze_account(account_id: str, amount_usd: float, counterparty: str, reason: str) -> dict[str, Any]:
    return ops.freeze_account(account_id, amount_usd, counterparty, reason)


@mcp.tool(description="File a suspicious transaction report (STR) for an account.")
def file_str(account_id: str, summary: str) -> dict[str, Any]:
    return ops.file_str(account_id, summary)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
```

`account_ops/Dockerfile`：

```dockerfile
FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir "mcp==1.30.0"
COPY account_ops /app/account_ops
ENV PYTHONUNBUFFERED=1
EXPOSE 8200
CMD ["python", "-m", "account_ops.server"]
```

`.dockerignore`（避免把 `.venv` 等送進 build context）：

```
.venv
.git
var
docs
**/__pycache__
.pytest_cache
.ruff_cache
.mypy_cache
.env
```

- [ ] **Step 4: 確認通過並 Commit**

Run: `uv run pytest tests/test_account_ops.py -q && uv run ruff format account_ops tests && uv run ruff check account_ops tests`
Expected: 1 passed

```bash
git add account_ops tests/test_account_ops.py .dockerignore
git commit -m "feat: add simulated account_ops MCP server"
```

---

### Task 16: 組裝 gateway 映像、compose、Keycloak 與 LiteLLM 設定

**Files:**
- Create: `Dockerfile`
- Modify: `docker-compose.yml`、`keycloak/realm-governance.json`、`litellm/config.yaml`、`.env.example`、`.env`
- Delete: `litellm/governance_hook.py`

- [ ] **Step 1: gateway 映像 `Dockerfile`**

```dockerfile
FROM docker.io/litellm/litellm:v1.105.1
# 映像內沒有 pip；執行期相依已在 LiteLLM 的 venv，直接以 PYTHONPATH 載入原始碼
COPY src/ai_gov_gateway /opt/ai-gov-gateway/ai_gov_gateway
ENV PYTHONPATH=/opt/ai-gov-gateway
```

保留基底映像的 `ENTRYPOINT`（`docker/prod_entrypoint.sh`），由 compose 提供 `--config` 參數，讓 LiteLLM 照常做 Prisma migration。

- [ ] **Step 2: Keycloak realm**

新增 carol、`agw-cli`、`account-ops`，並讓 `ai-gateway` 的 token exchange 可交換出 `account-ops` audience：

```bash
python3 - <<'EOF'
import json, pathlib
p = pathlib.Path("keycloak/realm-governance.json"); realm = json.loads(p.read_text())

def aud(name, target):
    return {"name": name, "protocol": "openid-connect", "protocolMapper": "oidc-audience-mapper",
            "config": {"included.client.audience": target, "access.token.claim": "true", "id.token.claim": "false"}}

if not any(u["username"] == "carol" for u in realm["users"]):
    realm["users"].append({"username": "carol", "enabled": True, "email": "carol@example.test",
        "emailVerified": True, "firstName": "Carol", "lastName": "Lead",
        "credentials": [{"type": "password", "value": "carol-test-pw", "temporary": False}],
        "realmRoles": ["analyst", "compliance_supervisor"]})
clients = {c["clientId"]: c for c in realm["clients"]}
if "agw-cli" not in clients:
    realm["clients"].append({"clientId": "agw-cli", "publicClient": True, "standardFlowEnabled": False,
        "directAccessGrantsEnabled": False,
        "attributes": {"oauth2.device.authorization.grant.enabled": "true"},
        "protocolMappers": [aud("aud-ai-gateway", "ai-gateway")]})
if "account-ops" not in clients:
    realm["clients"].append({"clientId": "account-ops", "publicClient": False,
        "secret": "account-ops-test-secret", "standardFlowEnabled": False, "directAccessGrantsEnabled": False})
gateway = clients["ai-gateway"]
if not any(m["name"] == "aud-account-ops" for m in gateway["protocolMappers"]):
    gateway["protocolMappers"].append(aud("aud-account-ops", "account-ops"))
p.write_text(json.dumps(realm, indent=2) + "\n"); print("realm updated")
EOF
```

`.env.example` 的測試值區塊新增一行，`.env` 也加入同一行：

```bash
CAROL_PASSWORD=carol-test-pw
```

- [ ] **Step 3: LiteLLM 設定**

`litellm/config.yaml` 的 `mcp_servers` 下、`probe` 之後新增：

```yaml
  account_ops:
    url: "http://account_ops:8200/mcp"
    transport: "http"
    auth_type: oauth2_token_exchange
    token_exchange_endpoint: "http://keycloak:8080/realms/governance/protocol/openid-connect/token"
    client_id: "ai-gateway"
    client_secret: os.environ/AI_GATEWAY_CLIENT_SECRET
    audience: "account-ops"
```

`guardrails` 區塊改為：

```yaml
guardrails:
  - guardrail_name: governance
    litellm_params:
      # 載入此模組時也會把 /governance 路由掛到 LiteLLM 的 app
      guardrail: ai_gov_gateway.plugin.GovernanceGuardrail
      mode: pre_mcp_call
      default_on: true
```

刪除 M1 的驗證版 hook：

```bash
git rm litellm/governance_hook.py
```

- [ ] **Step 4: `docker-compose.yml`**

把 `litellm` 服務整段替換為 `gateway`，並新增 `opa`、`account_ops`：

```yaml
  account_ops:
    build:
      context: .
      dockerfile: account_ops/Dockerfile

  opa:
    image: docker.io/openpolicyagent/opa:1.21.1
    # 不對外開 port：只有 gateway 能透過內部網路查詢
    command: ["run", "--server", "--addr", "0.0.0.0:8181", "--watch", "--log-level", "error", "/policies"]
    volumes:
      - ./policies:/policies:ro

  gateway:
    build:
      context: .
      dockerfile: Dockerfile
    command: ["--config", "/app/config.yaml", "--port", "4000"]
    environment:
      DATABASE_URL: postgresql://litellm:${POSTGRES_PASSWORD}@postgres:5432/litellm
      LITELLM_MASTER_KEY: ${LITELLM_MASTER_KEY}
      GOOGLE_API_KEY: ${GOOGLE_API_KEY}
      AI_GATEWAY_CLIENT_SECRET: ${AI_GATEWAY_CLIENT_SECRET}
      STORE_MODEL_IN_DB: "True"
      GOV_DATABASE_URL: postgresql://litellm:${POSTGRES_PASSWORD}@postgres:5432/governance
      GOV_OPA_URL: http://opa:8181
    volumes:
      - ./litellm/config.yaml:/app/config.yaml:ro
    ports:
      - "4000:4000"
    depends_on:
      postgres:
        condition: service_healthy
      opa:
        condition: service_started
      keycloak:
        condition: service_started
```

- [ ] **Step 5: 重建並啟動**

Keycloak 的匯入策略是 `IGNORE_EXISTING`，realm 變更需重建容器：

```bash
docker compose rm -sf keycloak
docker compose up -d --build --remove-orphans
for i in $(seq 1 60); do curl -sf http://localhost:4000/health/liveliness >/dev/null && echo up && break; sleep 3; done
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:4000/governance/approvals
docker compose logs gateway 2>&1 | grep -iE "error|governance|guardrail" | grep -v "Errno 101" | tail -10
docker compose ps --format '{{.Service}} {{.State}}'
```

Expected: `up`；`/governance/approvals` 回 `401`（路由已掛上、要求 token）；六個服務都是 `running`（keycloak、probe_mcp、account_ops、postgres、opa、gateway）

若 401 以外（例如 404），表示 plugin 未載入：檢查 `docker compose exec -T gateway sh -c 'echo $PYTHONPATH; ls /opt/ai-gov-gateway/ai_gov_gateway'` 與 gateway 日誌中 guardrail 載入錯誤。

- [ ] **Step 6: Commit**

```bash
git add Dockerfile docker-compose.yml keycloak/realm-governance.json litellm/config.yaml .env.example
git commit -m "feat: run governance plugin inside the LiteLLM gateway image"
```

---

### Task 17: 模擬 agent 與端到端示範

**Files:**
- Modify: `scripts/make_keys.py`、`scripts/call_whoami.py`
- Create: `scripts/agent_sim.py`、`scripts/demo_m2.py`
- Test: `tests/test_agent_sim.py`

- [ ] **Step 1: 寫失敗的測試 `tests/test_agent_sim.py`**

```python
from scripts.agent_sim import parse_result


def test_allowed_result_keeps_text():
    result = parse_result(False, '{"status": "frozen"}')
    assert (result.status, result.body) == ("allowed", {"text": '{"status": "frozen"}'})


def test_governance_message_is_parsed_after_prefix():
    text = 'Error: {"governance": "pending_approval", "approval_id": "apr_1", "reasons": ["x"]}'
    result = parse_result(True, text)
    assert result.status == "pending_approval"
    assert result.body["approval_id"] == "apr_1"


def test_other_errors():
    assert parse_result(True, "Error: upstream failed").status == "error"
```

- [ ] **Step 2: 確認失敗**

Run: `uv run pytest tests/test_agent_sim.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'scripts.agent_sim'`

- [ ] **Step 3: 更新 key 與預設 key**

`scripts/make_keys.py` 的 `KEYS` 前新增：

```python
MCP_ACCESS = {
    "object_permission": {
        "mcp_servers": ["probe", "account_ops"],
        "mcp_tool_permissions": {
            "probe": ["whoami", "echo"],
            "account_ops": ["add_to_watchlist", "freeze_account", "file_str"],
        },
    },
}
```

並在 `KEYS` 中新增兩把 key（`kyt-agent@1.5` 刻意不登記在 `policies/agents/data.json`）：

```python
    "kyt-agent@1.4": MCP_ACCESS,
    "kyt-agent@1.5": MCP_ACCESS,
```

`scripts/call_whoami.py` 的 `--key` 預設值改為 `"kyt-agent@1.4"`。

Run: `uv run python -m scripts.make_keys && uv run python -m scripts.call_whoami --user alice | head -3`
Expected: `keys:` 含 `kyt-agent@1.4`、`kyt-agent@1.5`；`tools:` 含 `probe-whoami` 與三個 `account_ops-…` tool；`isError: False`（`probe.whoami` 為 low 風險，V4 的 OBO 仍正常）

- [ ] **Step 4: 實作 `scripts/agent_sim.py`**

```python
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
```

- [ ] **Step 5: 實作 `scripts/demo_m2.py`**

```python
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
    return f"{result.status} {result.body.get('reasons', '')} {result.body.get('approval_id', '')}".strip()


async def main() -> int:
    watch = await call_tool(
        "kyt-agent@1.4", "alice", "add_to_watchlist", {"account_id": f"acct-{RUN}", "reason": "demo"}
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
        carol_self.status_code == 403 and carol_self.json().get("detail") == "self_approval_forbidden",
        f"{carol_self.status_code} {carol_self.json().get('detail')}",
    )

    api("POST", f"/approvals/{again.body.get('approval_id')}/approve", "bob", {})
    changed = await freeze(26000)
    check(
        "5 核准後改參數需重新審批",
        changed.status == "pending_approval" and changed.body.get("approval_id") != again.body.get("approval_id"),
        describe(changed),
    )

    sanctioned = await freeze(10, counterparty=SANCTIONED)
    check(
        "6 制裁地址送審",
        sanctioned.status == "pending_approval" and "sanctioned_counterparty" in sanctioned.body.get("reasons", []),
        describe(sanctioned),
    )

    report = await call_tool(
        "kyt-agent@1.4", "alice", "file_str", {"account_id": f"acct-{RUN}", "summary": "demo"}
    )
    check(
        "7 STR 一律送審",
        report.status == "pending_approval" and report.body.get("reasons") == ["str_always_reviewed"],
        describe(report),
    )

    unregistered = await freeze(5000, key="kyt-agent@1.5")
    check(
        "8 未註冊版本被拒",
        unregistered.status == "denied" and unregistered.body.get("reasons") == ["unregistered_agent_version"],
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
    last_id, reasons = psql("SELECT id, reasons::text FROM audit_events ORDER BY id DESC LIMIT 1").split("|", 1)
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
        f"before={before} tampered={tampered['first_invalid_id']} restored={restored['first_invalid_id']}",
    )

    print(f"\n{sum(results)}/{len(results)} 通過")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
```

- [ ] **Step 6: 執行並確認**

Run: `uv run pytest tests/test_agent_sim.py -q && uv run python -m scripts.demo_m2 | tee var/m2-demo.txt`
Expected: 3 passed；示範輸出 `13/13 通過`

任一情境失敗時：先看 `docker compose logs gateway | grep -i governance | tail -20`，確認 hook 收到的 `server`、`tool` 與 policy 原因；修正程式後重跑。不要修改預期結果來讓情境通過。

- [ ] **Step 7: 手動驗證 device flow（選做）**

```bash
uv run agw login          # 在瀏覽器以 bob 登入（密碼見 realm 檔）
uv run agw approvals list
uv run agw audit verify
```

Expected: 列出 pending 審批；`稽核紀錄完整（N 筆）`

- [ ] **Step 8: Lint 與 Commit**

```bash
uv run ruff format scripts tests && uv run ruff check scripts tests
git add scripts tests/test_agent_sim.py
git commit -m "feat: add simulated agent and end-to-end governance demo"
```

---

### Task 18: gateway 契約的整合測試

**Files:**
- Create: `tests/integration/test_gateway_contract.py`

- [ ] **Step 1: 撰寫整合測試**

```python
"""升級 LiteLLM 前必跑：確認 plugin 掛載、hook 欄位與訊息傳遞仍成立。"""

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
    assert '"aud": "probe-mcp"' in result.body["text"]
```

- [ ] **Step 2: 執行並 Commit**

Run: `uv run pytest -m integration -q`
Expected: 14 passed（Postgres store 10 + 契約 4）

```bash
git add tests/integration/test_gateway_contract.py
git commit -m "test: add gateway contract tests for LiteLLM upgrades"
```

---

### Task 19: 文件

**Files:**
- Modify: `README.md`、`AGENTS.md`
- Modify（root repo）：`../AGENTS.md`
- Create（root repo）：`../docs/ai-gov-gateway/04-m2-governance.md`
- Modify（root repo）：`../docs/ai-gov-gateway/build_html.py`、`../docs/ai-gov-gateway/01-ai-llm-techniques.md`（§10、§12 的 M2 狀態）

- [ ] **Step 1: README**

更新以下段落，內容以實際結果為準：
- 「現況」：M2 完成
- 「架構」：換成 spec §3 的架構圖，加上 OPA、account_ops 與 `/governance` 路由
- 「服務」：新增 `gateway`（取代 `litellm`）、`opa`、`account_ops`
- 新增「治理操作」：`agw login`、`agw approvals …`、`agw audit verify`、`scripts.demo_m2`
- 新增「Policy 維護」：`policies/` 目錄結構、修改後須更新 `meta/data.json` 的 `policy_version`、`opa test` 指令
- 「開發」：`uv run pytest`、`uv run pytest -m integration`、`opa test`

- [ ] **Step 2: `AGENTS.md`**

- 開發階段表：M2 改為「完成」，新增 M3（kyt_agent 接入）為「未開始」
- 目錄結構：加入 `src/ai_gov_gateway/`、`policies/`、`account_ops/`、`Dockerfile`
- 工具鏈：`mypy src`、`uv run pytest -m integration`、`opa test` 指令
- 新增一條規則：升級 LiteLLM 前必須跑 `uv run pytest -m integration`

- [ ] **Step 3: Commit（ai-gov-gateway）**

```bash
git add README.md AGENTS.md
git commit -m "docs: document M2 governance service"
```

- [ ] **Step 4: root repo 的學習文件與索引**

- `../AGENTS.md` 專案索引：ai-gov-gateway 狀態改為「M2 完成」
- 新增 `../docs/ai-gov-gateway/04-m2-governance.md`：M2 的設計取捨（單一 process 的做法與理由、OPA vs Keycloak、兌現方式、版本 key、fail-closed）、`policies/` 的規則、實測結果（引用 `var/m2-demo.txt` 的 13 個情境輸出）、踩過的坑（含 Rego 的 undefined fail-open）。依 `docs/AGENTS` 慣例使用 `**是什麼**`、`**學到的重點**` 等標籤，程式碼連結附行號
- `build_html.py` 的 `DOCS` 加入 `Doc("04-m2-governance.md", "m2", "M2 治理")`
- `01-ai-llm-techniques.md` §10、§12：把「M2 實作」改為已完成，連到 04

```bash
cd .. && uv run --with markdown --with pymdown-extensions python docs/ai-gov-gateway/build_html.py
git add AGENTS.md docs/ai-gov-gateway
git commit -m "docs: add ai-gov-gateway M2 learning notes"
```

Expected: 建置成功、無殘留 `.md` 連結錯誤

---

## Self-Review 對照

| Spec 章節 | 對應 Task |
| --------- | --------- |
| §2 決策（單一 process、OPA、兌現方式、版本 key、fail-closed） | 1、9、12、16 |
| §3 架構與元件 | 2–16 |
| §4 決策流程、fingerprint、TTL | 3、9 |
| §5 OPA policy | 8 |
| §6 審批 API 與 CLI | 10、13、14 |
| §7 稽核 | 5、11 |
| §8 錯誤處理 | 7、9、12 |
| §9 測試與 13 個情境 | 各 task 單元測試、8、11、17、18 |
| 其他完成條件（README、AGENTS、學習文件） | 19 |
