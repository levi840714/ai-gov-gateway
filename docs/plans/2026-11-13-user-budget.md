# M5 Implementation Plan：依委託使用者限制 LLM 用量

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** LLM 路徑（`/v1/chat/completions`）驗證委託使用者的 token，並依（agent, 使用者）限制日、月預算、rpm、tpm 與單次輸出；在呼叫模型之前預留最壞成本，超過就拒絕並寫入 hash chain 稽核。

**Architecture:** 新的 `LlmGate`（`engine/llm_gate.py`）串起 M5 的檢查：拒絕 client 自帶的 end user 身份 → 驗證 token → 查 OPA（guardrail 設定與使用者額度）→ 輸出上限 → M4 的 guardrail → 估計成本 → 在帳本預留。判斷式是純函式（`engine/limits.py`），交易由 `PostgresStore` 的 `reserve()` 處理（以（agent, 使用者）的 advisory lock 序列化）。LiteLLM 的 success logging 事件以實際成本結算，failure hook 釋放。價格與 fallback 的對應在 plugin 層從 LiteLLM router 讀出後交給引擎，`engine/` 仍不 import LiteLLM。

**Tech Stack:** LiteLLM v1.105.1、OPA 1.21.1（Rego v1）、Postgres（psycopg）、Keycloak、pytest。

**Spec:** `docs/specs/2026-11-13-user-budget-design.md`；靜態檢查與待驗證項目：`docs/spikes/m5-user-budget-spike.md`

**Repo：** `/Users/liumengfu/Desktop/agentic_flow_practice/ai-gov-gateway`（以下簡稱 G），分支 `feat/m5-user-budget`。kyt_agent 的改動另開計畫（spec §12），本計畫不修改 kyt_agent。

**撰寫計畫前已確認的事實**
- LiteLLM 的 end user 預算在 auth 階段、以 client 自填的 id 檢查，guardrail 覆寫 `user` 無效（spike L1–L7），因此採 spec 的方案 B
- `x-litellm-api-key` 在認證時優先於 `Authorization`（L9）；hook 的 `data["secret_fields"]["raw_headers"]` 有原始 header（L8）
- `LlmGuardrail.guard_request()` 目前自己解析 agent 並查 OPA（`engine/guardrails/service.py:68–85`）；`OpaPolicyClient.llm_config(agent)` 只送 `{"agent": …}`（`engine/policy.py:37`）
- `GuardrailConfig` 的欄位缺漏時驗證失敗並視為 `policy_error`（`engine/guardrails/models.py:22`）；`GuardrailError.body()` 只有 `reasons`、`rules`、`field`
- 稽核的 `record_hash` 排除 `None`，新增選填欄位不改變既有紀錄的 hash；`AuditEventType` 是 Literal，新增事件要加進去（`engine/models.py:107`）
- `PostgresStore` 以 `_connect()` 取得連線，稽核以 `pg_advisory_xact_lock(AUDIT_LOCK_KEY)` 序列化（`engine/postgres.py`）
- `owner_of()` 以 `sha256(user_api_key_dict.token)` 識別呼叫者（`hook_adapter.py:47`）
- `fields.py` 的 `REQUEST_FIELDS` 含 `user` 與 `metadata`；`litellm_metadata`、`safety_identifier` 不在白名單
- kyt 的 LLM client 只送 virtual key（kyt_agent `graph/deps.py:_proxy_kwargs`），不送 `user` 與 `max_tokens`；Keycloak 的 `accessTokenLifespan` 為 300 秒
- Keycloak 測試帳號：`alice`（analyst）、`bob`（compliance_supervisor）、`carol`（兩者）
- `litellm.model_cost` 有 `gemini/gemini-3.8-flash` 與 `gpt-6-luna`（不含 `openai/` 前綴）的單價；probe 模型沒有單價（L10）

**待 Task 1 驗證的假設**（任一不成立時停下來回報，由 controller 依 spike 文件的「不成立時」調整後續 task，不要自行改設計）

| 編號 | 假設 | 用在 |
| ---- | ---- | ---- |
| B1 | hook 的 `secret_fields.raw_headers` 含 client 的 `authorization`（streaming 也是） | Task 3、9 |
| B2 | `x-litellm-api-key` 認證成功，JWT 不被轉送給 provider | Task 3、10 |
| B3 | guardrail 收到 `async_log_success_event`，`litellm_call_id` 與 pre-call 相同，帶實際成本與 key 雜湊；streaming、fallback 各一次 | Task 9 |
| B4 | router 重試的每次失敗都觸發 `async_log_failure_event` | Task 9（不用它釋放） |
| B5 | rate limit fallback 的第二個 `call_id` 收不到事件 | Task 5（TTL） |
| B6 | streaming 中斷時的事件與成本 | Task 13（已知限制） |
| B7 | Gemini 的 `max_tokens` 涵蓋 reasoning token | Task 6 |
| B8 | 請求 JSON 的字元數不低於 `prompt_tokens` | Task 6 |
| B9 | key 接受 `budget_duration: "1mo"` | Task 10 |
| B10 | 改寫的 `max_tokens` 會送到 provider，fallback 沿用 | Task 8、9 |

**通則**
- 指令在 G 的根目錄執行
- 不輸出 `.env`、`var/keys.json`、API key、密碼或 token 原文
- 每個 task 結束：`uv run pytest -q && uv run ruff format src tests scripts probe_llm && uv run ruff check . && uv run mypy src`
- 動到 `policies/` 時另跑：`docker run --rm -v "$PWD/policies:/policies:ro" openpolicyagent/opa:1.21.1 test /policies`，以及 `fmt --list`、`check --strict`，並更新 `policies/meta/data.json` 的 `policy_version`
- commit 訊息結尾加上 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
- 註解只說明「為什麼」，每段不超過兩行
- 金額一律用 `Decimal`（Postgres `numeric`），不用 float 累加

**上線順序**：M5 合併後，沒有帶使用者 token 的 LLM 請求一律 401。kyt_agent 的 v1.6（spec §12）要在同一次部署切換；在那之前，治理模式的 kyt 只能用 M4 的 gateway。

---

## File Structure

| 檔案 | 職責 |
| ---- | ---- |
| `scripts/spikes/m5_budget_probe.py`、`scripts/spikes/m5_spike.py`、`litellm/config.spike.yaml`、`docker-compose.spike.yml` | Task 1 的驗證程式（沿用 M4 的疊加方式） |
| `docs/spikes/m5-findings.md` | B1–B10 的結論 |
| `src/ai_gov_gateway/engine/models.py` | 事件類型 `llm_access` |
| `src/ai_gov_gateway/engine/guardrails/models.py` | `GuardrailError` 的 `limit` 欄位；`GuardrailConfig.limits` |
| `src/ai_gov_gateway/engine/limits.py`（新） | `UsageLimits`、`Estimate`、`UsageSnapshot`、`LimitDenial`；期別計算與額度判斷（純函式） |
| `src/ai_gov_gateway/engine/ledger.py`（新） | `UsageLedger` protocol、`ReservationRequest`、`Settlement`、`LedgerUnavailable`、`DuplicateReservation` |
| `src/ai_gov_gateway/engine/postgres.py` | `llm_usage` 表；`reserve()`、`settle()`、`release()` |
| `src/ai_gov_gateway/engine/pricing.py`（新） | `PriceTable`：由模型清單、fallback 與單價建出每個別名的最高單價；`estimate()` |
| `src/ai_gov_gateway/engine/llm_gate.py`（新） | `LlmGate.admit()`：M5 的處理順序、拒絕的稽核 |
| `src/ai_gov_gateway/engine/guardrails/service.py` | 拆成 `resolve()`（agent 與 OPA 設定）與 `guard_request()`（M4 的處理） |
| `src/ai_gov_gateway/engine/guardrails/fields.py` | `end_user_field()`：找出 body 的 `user`、`metadata.user_id`、`metadata.user`（由 `LlmGate` 拒絕並寫稽核） |
| `src/ai_gov_gateway/engine/policy.py` | `llm_config(agent, user)` |
| `src/ai_gov_gateway/hook_adapter.py` | `user_token_from_hook()`、`end_user_header()`、`settlement_from_log()` |
| `src/ai_gov_gateway/plugin.py` | pre-call 改用 `LlmGate`；success logging 結算；failure hook 釋放 |
| `src/ai_gov_gateway/runtime.py`、`config.py` | 建立 `LlmGate` 與 `PriceTable`；`GOV_BUDGET_TIMEZONE`、`GOV_BUDGET_RESERVATION_TTL_SECONDS` |
| `policies/governance_llm.rego`、`policies/governance_llm_test.rego`、`policies/agents/data.json`、`policies/meta/data.json` | `llm_limits` 等級、嚴格預設、`limits` 輸出 |
| `litellm/config.yaml` | probe 模型的測試單價（`model_info`） |
| `scripts/make_keys.py` | key 的 `budget_duration`、`max_budget` |
| `keycloak/realm-governance.json` | 測試帳號 `erin`（角色 `llm_quota_probe`） |
| `scripts/llm_sim.py`、`scripts/demo_m4.py`、`scripts/rate_limit_check.py`、`tests/integration/test_llm_guardrail_contract.py` | LLM 請求改為帶使用者 token |
| `scripts/demo_m5.py`（新） | M5 的端到端情境 |
| `tests/test_limits.py`、`tests/test_pricing.py`、`tests/test_llm_gate.py`（新）；`tests/test_hook_adapter.py`、`tests/test_guardrail_fields.py`、`tests/test_guardrail_service.py`、`tests/test_policy_client.py`、`tests/test_audit.py`、`tests/fakes.py` | 單元測試 |
| `tests/integration/test_usage_ledger.py`（新）、`tests/integration/test_llm_budget_contract.py`（新） | 整合測試 |

---

### Task 1：執行期驗證（B1–B10）

**Files:**
- Create: `scripts/spikes/m5_budget_probe.py`、`scripts/spikes/m5_spike.py`、`docs/spikes/m5-findings.md`
- Modify: `litellm/config.spike.yaml`、`docker-compose.spike.yml`

- [ ] **Step 1: 只記錄的 guardrail**

`scripts/spikes/m5_budget_probe.py`：繼承 `CustomGuardrail`，`mode: [pre_call, post_call]`，在以下方法各寫一行 `M5SPIKE` JSON 日誌（遞增的 `seq`、方法名、`litellm_call_id`）：

| 方法 | 記錄 |
| ---- | ---- |
| `async_pre_call_hook` | `secret_fields` 是否存在、`raw_headers` 的鍵名清單（**只記鍵名**）、是否有 `authorization`、其值是否為三段式 JWT（只記布林）；`data` 的 `max_tokens` |
| `async_log_success_event` | `kwargs["response_cost"]`、`standard_logging_object` 的 `response_cost`、`total_tokens`、`metadata.user_api_key_hash` 的 sha256 前 8 碼、`model`、`stream` |
| `async_log_failure_event` | `model`、例外類別名稱 |
| `async_post_call_failure_hook` | 例外類別名稱 |
| `async_post_call_success_hook`、`async_post_call_streaming_iterator_hook` | 方法名 |

pre-call 另外把 `data["max_tokens"]` 設為 `17`（B10）。不記錄任何 token 或 key 原文。

- [ ] **Step 2: 驗證腳本**

`scripts/spikes/m5_spike.py` 依序送出（使用 `kyt-agent@1.5` key 放 `x-litellm-api-key`、alice 的 token 放 `Authorization`、model `probe-primary`）：

1. 非 streaming 一次、streaming 一次（B1、B2、B3）
2. `probe_llm` 的請求紀錄：確認 header 沒有 `authorization` 的 JWT（B2）、`max_tokens` 為 17（B10）
3. `PROBE_FAIL_MODELS=probe-primary` 下送一次：主力 503 → fallback（B3 的 fallback、B4、B10 的 fallback）
4. 以 `tpm_limit=1` 的臨時 key 觸發 rate limit fallback（B5），結束後刪除該 key
5. streaming 請求讀到第一個 chunk 後關閉連線（B6）
6. `/key/update` 對臨時 key 設 `budget_duration: "1mo"`，讀回 `budget_reset_at`（B9），結束後刪除
7. 對照 `LiteLLM_SpendLogs`：同一個 `call_id` 的 `spend` 與日誌的 `response_cost`（B3）

B7、B8 需要真實 Gemini，放在 Task 12。

- [ ] **Step 3: 執行並記錄**

```bash
docker compose -f docker-compose.yml -f docker-compose.spike.yml up -d --build gateway
uv run python -m scripts.spikes.m5_spike
docker compose logs gateway | grep M5SPIKE > var/m5-spike.log
docker compose up -d --build gateway   # 回到正式設定
```

`docs/spikes/m5-findings.md`：比照 `m4-findings.md`，一個結論表（B1–B6、B9、B10，標明成立與否與證據），加上「對後續 task 的影響」。不貼日誌原文、不貼 header 值。

- [ ] **Step 4: Commit**

```bash
git add scripts/spikes/m5_*.py litellm/config.spike.yaml docker-compose.spike.yml docs/spikes/m5-findings.md
git commit -m "spike: verify LiteLLM hooks for per-user LLM budgets"
```

---

### Task 2：稽核事件 `llm_access` 與錯誤的 `limit` 欄位

**Files:**
- Modify: `src/ai_gov_gateway/engine/models.py`、`src/ai_gov_gateway/engine/guardrails/models.py`
- Test: `tests/test_audit.py`、`tests/test_guardrail_service.py`

- [ ] **Step 1: 寫失敗的測試**

```python
# tests/test_audit.py
def test_llm_access_event_verifies():
    event = AuditEvent(
        event_type="llm_access",
        agent_id="kyt-agent",
        agent_version="1.6",
        key_alias="kyt-agent@1.6",
        user_sub="alice-sub",
        username="alice",
        decision="deny",
        reasons=["user_budget_exceeded"],
        details={"call_id": "c1", "tier": "default", "limit": {"type": "budget_day", "limit_usd": "2.0"}},
    )
    record = build_record(record_id=1, prev_hash=GENESIS_HASH, ts=NOW, event=event)
    assert first_invalid([record]) is None


# tests/test_guardrail_service.py
def test_error_body_includes_limit():
    error = GuardrailError(403, "user_budget_exceeded", limit={"type": "budget_day", "resets_at": "x"})
    assert error.body() == {
        "governance": "denied",
        "reasons": ["user_budget_exceeded"],
        "limit": {"type": "budget_day", "resets_at": "x"},
    }
```

- [ ] **Step 2: 確認失敗**：`uv run pytest tests/test_audit.py tests/test_guardrail_service.py -q`

- [ ] **Step 3: 實作**
- `AuditEventType` 加入 `"llm_access"`
- `GuardrailError.__init__` 加入 `limit: dict[str, Any] | None = None`，`body()` 有值時加入 `"limit"`
- 稽核 `details` 的金額以字串存（`str(Decimal)`），避免 `json` 欄位與 hash 的數值表示不一致

- [ ] **Step 4: 通過並 commit**：`git commit -m "feat: add llm_access audit event and limit error details"`

---

### Task 3：取出使用者 token、拒絕自帶的 end user 身份

**Files:**
- Modify: `src/ai_gov_gateway/hook_adapter.py`、`src/ai_gov_gateway/engine/guardrails/fields.py`
- Test: `tests/test_hook_adapter.py`、`tests/test_guardrail_fields.py`

- [ ] **Step 1: 寫失敗的測試**

```python
# tests/test_hook_adapter.py
def _data(**headers: str) -> dict:
    return {"secret_fields": {"raw_headers": headers}}


def test_user_token_from_raw_authorization():
    assert user_token_from_hook(_data(authorization="Bearer eyJ.a.b")) == "eyJ.a.b"


def test_user_token_missing_when_no_secret_fields():
    assert user_token_from_hook({}) is None
    assert user_token_from_hook(_data()) is None


def test_user_token_header_name_is_case_insensitive():
    assert user_token_from_hook(_data(Authorization="Bearer t")) == "t"


@pytest.mark.parametrize("name", ["x-litellm-customer-id", "X-LiteLLM-End-User-Id"])
def test_end_user_header_detected(name):
    assert end_user_header(_data(**{name: "someone"})) == name.lower()


def test_no_end_user_header():
    assert end_user_header(_data(authorization="Bearer t")) is None


# tests/test_guardrail_fields.py
@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"user": "alice"}, "user"),
        ({"metadata": {"user_id": "x"}}, "metadata.user_id"),
        ({"metadata": {"user": "x"}}, "metadata.user"),
        ({"user": ""}, "user"),  # 空字串 LiteLLM 雖然不採用，仍視為自帶身份
    ],
)
def test_end_user_field_detected(body, field):
    assert end_user_field(body) == field


def test_no_end_user_field():
    assert end_user_field({"model": "m", "messages": [], "metadata": {"tag": "x"}}) is None
    assert end_user_field(None) is None
```

- [ ] **Step 2: 確認失敗**

- [ ] **Step 3: 實作**

`hook_adapter.py`：

```python
# LiteLLM 不論設定一律讀這兩個 header 當 end user id（spike L2），client 帶了就拒絕
END_USER_HEADERS = ("x-litellm-customer-id", "x-litellm-end-user-id")


def _raw_headers(data: Mapping[str, Any]) -> dict[str, str]:
    """docs/spikes/m5-findings.md B1：原始 header 只在 secret_fields，proxy_server_request 已遮蔽。"""
    secret = data.get("secret_fields")
    headers = secret.get("raw_headers") if isinstance(secret, Mapping) else None
    if not isinstance(headers, Mapping):
        return {}
    return {str(k).lower(): v for k, v in headers.items() if isinstance(v, str)}


def user_token_from_hook(data: Mapping[str, Any]) -> str | None:
    return _bearer(_raw_headers(data).get("authorization"))


def end_user_header(data: Mapping[str, Any]) -> str | None:
    headers = _raw_headers(data)
    return next((name for name in END_USER_HEADERS if name in headers), None)
```

`fields.py`：新增 `end_user_field(body: Mapping[str, Any] | None) -> str | None`，依序檢查 `user`、`metadata.user_id`、`metadata.user`（`metadata` 是字串時以 JSON 解析，與 LiteLLM 相同，spike L2）。`REQUEST_FIELDS` 保留 `user`，加註「由 `LlmGate` 以 `end_user_override` 拒絕並寫稽核」：在 `check_request` 直接拒絕就不會留下稽核。

- [ ] **Step 4: 通過並 commit**：`git commit -m "feat: read user token from raw headers and reject client end-user ids"`

---

### Task 4：額度模型與 OPA

**Files:**
- Create: `src/ai_gov_gateway/engine/limits.py`（先放模型，判斷在 Task 7）
- Modify: `src/ai_gov_gateway/engine/guardrails/models.py`、`src/ai_gov_gateway/engine/policy.py`、`policies/governance_llm.rego`、`policies/governance_llm_test.rego`、`policies/agents/data.json`、`policies/meta/data.json`、`tests/fakes.py`
- Test: `tests/test_policy_client.py`、`tests/test_limits.py`

- [ ] **Step 1: Rego 的失敗測試**

`policies/governance_llm_test.rego` 新增（以 `with data.agents as …` 與 `with input as …` 組合）：

| 測試 | 預期 |
| ---- | ---- |
| `test_limits_first_matching_tier` | carol（analyst、compliance_supervisor）套用清單中第一個符合的等級 |
| `test_limits_wildcard_tier` | alice 套用 `"*"` 等級 |
| `test_limits_null_without_matching_tier` | 清單沒有 `"*"`、使用者角色不符 → `llm.limits == null` |
| `test_limits_strict_default_when_unconfigured` | agent 沒有 `llm_limits` → `strict_limits` |
| `test_llm_undefined_when_tier_invalid` | 等級缺 `rpm`、`budget_usd` 不是物件、`budget_usd` 沒有 `day` 也沒有 `month`、數值為負 → `not llm` |
| `test_llm_undefined_when_limits_not_array` | `llm_limits` 是物件 → `not llm` |
| `test_roles_not_array_matches_only_wildcard` | `input.user.roles` 不是陣列 → 只有 `"*"` 會符合 |

- [ ] **Step 2: 確認失敗**：`docker run --rm -v "$PWD/policies:/policies:ro" openpolicyagent/opa:1.21.1 test /policies`

- [ ] **Step 3: Rego 實作**

`policies/governance_llm.rego`：

```rego
# 已註冊但未設定 llm_limits 的 agent 套用最嚴格的額度；等級欄位不完整時 llm 為 undefined
strict_limits := {
	"tier": "strict",
	"role": "*",
	"budget_usd": {"day": 0.5, "month": 5},
	"rpm": 10,
	"tpm": 50000,
	"max_output_tokens": 2048,
}

configured_tiers := agent.llm_limits if is_array(agent.llm_limits)

limits_invalid if {
	agent.llm_limits
	not configured_tiers
}

limits_invalid if {
	some tier in configured_tiers
	not valid_tier(tier)
}

valid_tier(tier) if {
	is_string(tier.tier)
	is_string(tier.role)
	valid_budget(tier.budget_usd)
	positive_int(tier.rpm)
	positive_int(tier.tpm)
	positive_int(tier.max_output_tokens)
}

valid_budget(budget) if {
	is_object(budget)
	count(budget) > 0
	every period, amount in budget {
		period in {"day", "month"}
		is_number(amount)
		amount > 0
	}
}

positive_int(value) if {
	is_number(value)
	value == floor(value)
	value > 0
}

tier_applies(tier) if tier.role == "*"

tier_applies(tier) if {
	is_array(input.user.roles)
	tier.role in input.user.roles
}

matching_tiers := [tier | some tier in configured_tiers; tier_applies(tier)]

llm_limits := strict_limits if {
	not agent.llm_limits
} else := matching_tiers[0] if {
	count(matching_tiers) > 0
} else := null
```

`llm` 加入 `"limits": llm_limits`，並在規則條件加上 `not limits_invalid`。

`policies/agents/data.json` 的 `kyt-agent` 加入 `llm_limits`（順序：`llm_quota_probe` 測試等級、`compliance_supervisor`、`"*"`）：

```json
"llm_limits": [
  {"tier": "quota_probe", "role": "llm_quota_probe", "budget_usd": {"day": 0.002, "month": 0.01}, "rpm": 5, "tpm": 5000, "max_output_tokens": 64},
  {"tier": "supervisor", "role": "compliance_supervisor", "budget_usd": {"day": 5.0, "month": 50.0}, "rpm": 30, "tpm": 150000, "max_output_tokens": 8192},
  {"tier": "default", "role": "*", "budget_usd": {"day": 2.0, "month": 20.0}, "rpm": 20, "tpm": 150000, "max_output_tokens": 8192}
]
```

`quota_probe` 只給整合測試與 demo 的 `erin`（Task 10），額度刻意很小。`policy_version` 改為當天日期加序號。

- [ ] **Step 4: Python 模型與 client 的失敗測試**

```python
# tests/test_limits.py
def test_limits_parse_decimal_budget():
    limits = UsageLimits.model_validate(
        {"tier": "default", "role": "*", "budget_usd": {"day": 2.0}, "rpm": 20, "tpm": 150000, "max_output_tokens": 8192}
    )
    assert limits.budget_usd == {"day": Decimal("2.0")}


def test_limits_reject_unknown_period():
    with pytest.raises(ValidationError):
        UsageLimits.model_validate({**VALID, "budget_usd": {"week": 1}})


# tests/test_policy_client.py
async def test_llm_config_sends_user_and_parses_limits():
    # MockTransport 檢查 request body 的 input.user 與 input.agent，回傳含 limits 的結果
    ...
    assert config.limits.tier == "default"


async def test_llm_config_missing_limits_key_is_policy_error():
    ...  # 回傳的結果沒有 limits 鍵 → PolicyError("policy_error")


async def test_llm_config_null_limits_is_allowed():
    ...  # limits 為 null → config.limits is None
```

- [ ] **Step 5: 實作**

`engine/limits.py`：

```python
Period = Literal["day", "month"]


class UsageLimits(BaseModel):
    """OPA governance.llm 的 limits；欄位缺漏時驗證失敗，由 policy client 視為 policy_error。"""

    tier: str
    budget_usd: dict[Period, Decimal] = Field(min_length=1)
    rpm: int = Field(gt=0)
    tpm: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
```

`GuardrailConfig` 加入 `limits: UsageLimits | None`（**沒有預設值**：OPA 少了這個鍵就是 `policy_error`）。`OpaPolicyClient.llm_config(agent, user)` 送 `{"agent": …, "user": user.model_dump()}`；`GuardrailPolicy` protocol 與 `tests/fakes.py` 的 `FakeGuardrailPolicy`、`guardrail_config()` 同步（fake 記錄最後一次收到的 user，`guardrail_config()` 預設帶一份 `default` 等級）。

- [ ] **Step 6: 通過並 commit**

```bash
git add policies src/ai_gov_gateway/engine tests
git commit -m "feat: per-user LLM limit tiers in OPA"
```

---

### Task 5：用量帳本

**Files:**
- Create: `src/ai_gov_gateway/engine/ledger.py`、`tests/integration/test_usage_ledger.py`
- Modify: `src/ai_gov_gateway/engine/postgres.py`、`tests/fakes.py`

- [ ] **Step 1: 介面**

```python
# engine/ledger.py
class LedgerUnavailable(Exception):
    """帳本無法讀寫；pre-call 一律拒絕（budget_unavailable）。"""


class DuplicateReservation(Exception):
    """同一把 key 重複使用 call_id；call_id 可由 client 以 x-litellm-call-id 指定。"""


class ReservationRequest(BaseModel):
    owner: str
    call_id: str
    agent_id: str
    user_sub: str
    model: str
    estimate: Estimate          # engine/limits.py：usd 與 tokens
    windows: UsageWindows       # engine/limits.py：本日、本月的起點與 60 秒視窗的起點
    now: datetime
    expires_at: datetime


class Settlement(BaseModel):
    owner: str
    call_id: str
    cost_usd: Decimal
    tokens: int
    at: datetime


class UsageLedger(Protocol):
    async def reserve(
        self, request: ReservationRequest, check: Callable[[UsageSnapshot], LimitDenial | None]
    ) -> LimitDenial | None:
        """在同一個交易中讀出用量、呼叫 check；沒有拒絕才寫入預留。"""
        ...

    async def settle(self, settlement: Settlement) -> bool: ...

    async def release(self, owner: str, call_id: str, at: datetime) -> bool: ...
```

`check` 是純函式（Task 7 的 `check_usage`），交易只負責一致性，判斷可以單獨測試。

- [ ] **Step 2: 整合測試（先寫）**

`tests/integration/test_usage_ledger.py`（`governance_test` 資料庫，沿用 `test_postgres_store.py` 的 fixture）：

| 測試 | 內容 |
| ---- | ---- |
| `test_reserve_inserts_when_check_passes` | check 回 None → 寫入一筆 `reserved` |
| `test_reserve_skips_insert_when_denied` | check 回 `LimitDenial` → 回傳該拒絕、表中沒有新資料 |
| `test_snapshot_counts_settled_and_live_reservations` | 一筆已結算（cost 0.01）、一筆未過期預留（0.05）、一筆過期預留、一筆已釋放 → snapshot 的本日金額 0.06 |
| `test_snapshot_separates_day_and_month` | 上個月、本月較早、今天各一筆 → day 只含今天，month 含本月 |
| `test_snapshot_minute_window` | 61 秒前與 30 秒前各一筆 → 請求數 1；token 以結算值或預留值計 |
| `test_minute_window_across_month_start` | 每月 1 日 00:00:30，前一個月 23:59:50 的請求仍算進 60 秒視窗 |
| `test_settle_updates_cost_even_after_release` | 釋放後才收到結算 → 狀態 `settled`、cost 寫入 |
| `test_release_does_not_override_settled` | 結算後釋放 → 仍是 `settled` |
| `test_settle_unknown_call_returns_false` | 沒有預留的 call_id → False，不寫入 |
| `test_duplicate_call_id_raises` | 同一 owner 重複 call_id → `DuplicateReservation`；不同 owner 可重複 |
| `test_concurrent_reservations_do_not_exceed_budget` | 20 個 `asyncio.gather` 的預留，每筆 0.03、日額度 0.25 → 正好 8 筆成功，本日佔用 ≤ 0.25 |
| `test_different_users_do_not_block` | 一位使用者的交易持有鎖時，另一位使用者的預留能完成（以 `asyncio.wait_for` 檢查） |
| `test_numeric_precision` | `Decimal("0.000000375")` 寫入讀回不變 |
| `test_unavailable_database_raises` | 錯誤的 URL → `LedgerUnavailable` |

- [ ] **Step 3: 確認失敗**：`uv run pytest -m integration tests/integration/test_usage_ledger.py -q`

- [ ] **Step 4: 實作**

`SCHEMA` 加入 spec §8.1 的 `llm_usage` 表與索引。`PostgresStore` 加上三個方法：

```python
# 與 AUDIT_LOCK_KEY 不同的命名空間：雙參數的 advisory lock，第二個參數是（agent, 使用者）
USAGE_LOCK_SPACE = 72_410_002

SNAPSHOT = """
SELECT
  coalesce(sum(CASE WHEN status = 'settled' THEN cost_usd
                    WHEN status = 'reserved' AND expires_at > %(now)s THEN reserved_usd
                    ELSE 0 END) FILTER (WHERE created_at >= %(day_start)s), 0) AS day_usd,
  coalesce(sum(CASE WHEN status = 'settled' THEN cost_usd
                    WHEN status = 'reserved' AND expires_at > %(now)s THEN reserved_usd
                    ELSE 0 END) FILTER (WHERE created_at >= %(month_start)s), 0) AS month_usd,
  count(*) FILTER (WHERE created_at > %(minute_start)s) AS minute_requests,
  coalesce(sum(CASE WHEN status = 'settled' THEN tokens
                    WHEN status = 'reserved' AND expires_at > %(now)s THEN reserved_tokens
                    ELSE 0 END) FILTER (WHERE created_at > %(minute_start)s), 0) AS minute_tokens,
  min(created_at) FILTER (WHERE created_at > %(minute_start)s) AS minute_oldest
FROM llm_usage
WHERE agent_id = %(agent_id)s AND user_sub = %(user_sub)s
  AND created_at >= least(%(month_start)s, %(minute_start)s)
"""


async def reserve(self, request, check):
    async with self._connect() as conn, conn.transaction():
        await conn.execute(
            "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
            (USAGE_LOCK_SPACE, f"{request.agent_id}:{request.user_sub}"),
        )
        snapshot = UsageSnapshot.model_validate(await (await conn.execute(SNAPSHOT, ...)).fetchone())
        denial = check(snapshot)
        if denial is None:
            await conn.execute(INSERT_RESERVATION, ...)  # UniqueViolation → DuplicateReservation
        return denial
```

- `settle()`：`UPDATE … SET status = 'settled', cost_usd, tokens, settled_at WHERE owner AND call_id`（不論原狀態），回傳是否有更新
- `release()`：`UPDATE … SET status = 'released' WHERE owner AND call_id AND status = 'reserved'`
- 連線與一般錯誤沿用 `_connect()` 的轉換，但 ledger 的呼叫端只認 `LedgerUnavailable`：在三個方法外層把 `StoreUnavailable`、`StoreError` 轉成 `LedgerUnavailable`
- `tests/fakes.py` 加入 `InMemoryLedger`（以 `asyncio.Lock` 模擬序列化，snapshot 規則與 SQL 相同），供 Task 7、8 的單元測試使用

- [ ] **Step 5: 通過並 commit**：`git commit -m "feat: usage ledger with reservations in the governance database"`

---

### Task 6：價格與成本估計

**Files:**
- Create: `src/ai_gov_gateway/engine/pricing.py`、`tests/test_pricing.py`
- Modify: `litellm/config.yaml`

- [ ] **Step 1: 寫失敗的測試**

```python
MODEL_LIST = [
    {"model_name": "kyt-main", "litellm_params": {"model": "gemini/gemini-3.8-flash"}},
    {"model_name": "kyt-main-fallback", "litellm_params": {"model": "openai/gpt-6-luna"}},
    {
        "model_name": "probe-primary",
        "litellm_params": {"model": "openai/probe-primary"},
        "model_info": {"input_cost_per_token": 1e-6, "output_cost_per_token": 1e-5},
    },
]
FALLBACKS = [{"kyt-main": ["kyt-main-fallback"]}]
COSTS = {
    "gemini/gemini-3.8-flash": {"input_cost_per_token": 7.5e-7, "output_cost_per_token": 3.75e-6},
    "gpt-6-luna": {"input_cost_per_token": 1e-7, "output_cost_per_token": 5e-7},
}


def test_alias_uses_highest_price_across_fallbacks():
    table = PriceTable.build(MODEL_LIST, FALLBACKS, COSTS)
    assert table.price("kyt-main") == ModelPrice(input=Decimal("7.5e-7"), output=Decimal("3.75e-6"))


def test_provider_prefix_is_stripped_when_not_found():
    table = PriceTable.build(MODEL_LIST, [], COSTS)
    assert table.price("kyt-main-fallback").input == Decimal("1e-7")


def test_model_info_overrides_cost_map():
    assert PriceTable.build(MODEL_LIST, [], COSTS).price("probe-primary").output == Decimal("1e-5")


def test_unknown_price_raises():
    with pytest.raises(PriceUnknown):
        PriceTable.build([{"model_name": "x", "litellm_params": {"model": "nope/x"}}], [], {}).price("x")


def test_fallback_without_price_makes_alias_unknown():
    # 有一個 fallback 查不到單價時無法保證上限，整個別名視為未知
    ...


def test_estimate_counts_json_chars_and_output_cap():
    messages = [{"role": "user", "content": "你好 hello"}]
    estimate = PriceTable.build(MODEL_LIST, FALLBACKS, COSTS).estimate("kyt-main", messages, None, 100)
    input_chars = len(json.dumps(messages, ensure_ascii=False))
    assert estimate.tokens == input_chars + 100
    assert estimate.usd == input_chars * Decimal("7.5e-7") + 100 * Decimal("3.75e-6")
```

- [ ] **Step 2: 確認失敗**

- [ ] **Step 3: 實作**

- `PriceTable.build(model_list, fallbacks, model_cost)`：每個別名取所有 deployment 與 fallback 鏈（遞迴、防循環）中最高的輸入與輸出單價；單價先看 deployment 的 `model_info`，再查 `model_cost[model]`，找不到時去掉 `<provider>/` 前綴再查
- `estimate(model, messages, tools, max_output_tokens) -> Estimate`：輸入估計為 `len(json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False))`；B8 不成立時乘上 findings 的係數（設為模組常數並註明出處）
- B7 不成立時，`estimate` 另加 reasoning 的上限（findings 決定數值）
- 只收 plain data，不 import LiteLLM；plugin 在 Task 9 從 `llm_router.model_list`、`llm_router.fallbacks`、`litellm.model_cost` 建立

`litellm/config.yaml` 的 probe 模型加上測試單價（讓整合測試與 demo 能用到預算）：

```yaml
  - model_name: probe-primary
    litellm_params: …
    # 測試單價：讓依使用者的預算在不花 LLM 費用的情況下也能驗證
    model_info: {input_cost_per_token: 0.000001, output_cost_per_token: 0.00001}
```

`probe-fallback` 同樣設定。README 中「probe 沒有定價」的說明在 Task 13 改寫。

- [ ] **Step 4: 通過並 commit**：`git commit -m "feat: price table and worst-case cost estimate"`

---

### Task 7：額度判斷

**Files:**
- Modify: `src/ai_gov_gateway/engine/limits.py`
- Test: `tests/test_limits.py`

- [ ] **Step 1: 寫失敗的測試**

```python
TZ = ZoneInfo("Asia/Taipei")
NOW = datetime(2026, 11, 13, 15, 0, tzinfo=UTC)  # 台北 23:00


def test_windows_use_local_calendar():
    windows = usage_windows(NOW, TZ)
    assert windows.day_start == datetime(2026, 11, 13, 0, 0, tzinfo=TZ)
    assert windows.month_start == datetime(2026, 11, 1, 0, 0, tzinfo=TZ)
    assert windows.minute_start == NOW - timedelta(seconds=60)


def test_window_after_local_midnight():
    windows = usage_windows(datetime(2026, 11, 13, 16, 30, tzinfo=UTC), TZ)  # 台北 11/14 00:30
    assert windows.day_start == datetime(2026, 11, 14, 0, 0, tzinfo=TZ)


def test_within_all_limits_passes():
    assert check_usage(snapshot(day_usd="1.0"), LIMITS, estimate("0.5", 1000), NOW, TZ) is None


def test_exactly_reaching_budget_passes():
    assert check_usage(snapshot(day_usd="1.5"), LIMITS, estimate("0.5", 1000), NOW, TZ) is None


def test_day_budget_exceeded():
    denial = check_usage(snapshot(day_usd="1.6"), LIMITS, estimate("0.5", 1000), NOW, TZ)
    assert denial.code == "user_budget_exceeded"
    assert denial.limit == {"type": "budget_day", "resets_at": "2026-11-14T00:00:00+08:00"}
    assert denial.details["usage"] == {"used_usd": "1.6", "estimate_usd": "0.5"}


def test_month_budget_checked_when_day_has_room(): ...


def test_rpm_exceeded_returns_retry_after():
    oldest = NOW - timedelta(seconds=48)
    denial = check_usage(snapshot(minute_requests=20, minute_oldest=oldest), LIMITS, estimate("0.01", 10), NOW, TZ)
    assert (denial.status, denial.code) == (429, "user_rate_limited")
    assert denial.limit == {"type": "rpm", "retry_after_seconds": 12}


def test_tpm_counts_estimate(): ...


def test_rpm_checked_before_budget():
    # 兩者都超過時回 rpm（429，可重試），避免短時間大量請求都被記成預算用完
    ...
```

（`LIMITS` 為 `budget_usd={"day": 2.0, "month": 20.0}`、`rpm=20`、`tpm=150000`。）

- [ ] **Step 2: 確認失敗**

- [ ] **Step 3: 實作**

```python
class Estimate(BaseModel):
    usd: Decimal
    tokens: int


class UsageWindows(BaseModel):
    day_start: datetime
    month_start: datetime
    minute_start: datetime


class UsageSnapshot(BaseModel):
    day_usd: Decimal
    month_usd: Decimal
    minute_requests: int
    minute_tokens: int
    minute_oldest: datetime | None


class LimitDenial(BaseModel):
    status: int
    code: str
    limit: dict[str, Any]
    details: dict[str, Any]


def usage_windows(now: datetime, tz: ZoneInfo) -> UsageWindows: ...


def check_usage(
    snapshot: UsageSnapshot, limits: UsageLimits, estimate: Estimate, now: datetime, tz: ZoneInfo
) -> LimitDenial | None:
    """依序檢查 rpm、tpm、日、月；等於上限時放行。"""
```

- `resets_at` 以 `tz` 的下一個日或月起點，ISO 格式含時區
- `retry_after_seconds = max(1, ceil(minute_oldest + 60s − now))`
- `details` 的金額以字串表示（Task 2）

- [ ] **Step 4: 通過並 commit**：`git commit -m "feat: per-user limit checks with calendar windows"`

---

### Task 8：`LlmGate`：M5 的處理順序

**Files:**
- Create: `src/ai_gov_gateway/engine/llm_gate.py`、`tests/test_llm_gate.py`
- Modify: `src/ai_gov_gateway/engine/guardrails/service.py`、`tests/test_guardrail_service.py`

- [ ] **Step 1: 拆開 `LlmGuardrail`**

`guard_request()` 目前做三件事：解析 agent、查 OPA、處理訊息。拆成：

```python
async def resolve(self, key_alias: str | None, user: UserIdentity) -> tuple[AgentRef, GuardrailConfig]:
    """名稱不符、未註冊、停用時寫 llm_access 稽核後丟 403；OPA 錯誤丟 503。"""


async def guard_request(
    self, *, agent: AgentRef, config: GuardrailConfig, key_alias: str | None,
    model: str | None, call_id: str | None, messages: list[Message],
) -> GuardedRequest:
    """M4 的大小檢查、注入、PII、spotlighting 與 llm_guardrail 稽核，行為不變。"""
```

`resolve()` 的拒絕改為先寫 `llm_access`（補上 M4 的已知限制）；稽核失敗時丟 503 `audit_write_failed`。既有 `tests/test_guardrail_service.py` 改為先呼叫 `resolve()` 再呼叫 `guard_request()`，斷言不變；新增 `test_unregistered_writes_llm_access_audit`、`test_suspended_writes_llm_access_audit`、`test_resolve_sends_user_to_policy`。

- [ ] **Step 2: `LlmGate` 的失敗測試**

`tests/test_llm_gate.py` 以 `FakeTokenVerifier`（測試內定義：固定 token 對應 `UserIdentity`，其他丟 `TokenError`）、`FakeGuardrailPolicy`、`InMemoryStore`、`InMemoryLedger`、`FakePiiDetector`、`FakeClock` 組出 gate：

| 測試 | 預期 |
| ---- | ---- |
| `test_admit_happy_path` | 回傳改寫後的訊息、`max_tokens` 補為等級上限、帳本一筆 `reserved`（估計值正確）、沒有 `llm_access` 稽核 |
| `test_end_user_override_denied_before_token` | header 與 body 欄位各一例：400 `end_user_override`，`field` 為 header 或欄位名稱；稽核 `llm_access`，`user_sub` 為 None；沒有驗證 token |
| `test_missing_token` | 401 `missing_user_token`；稽核有 agent 沒有 user；沒有查 OPA |
| `test_invalid_token` | 401 `invalid_user_token`；稽核 |
| `test_no_matching_tier` | `limits=None` → 403 `no_llm_quota`；稽核含 `policy_version` |
| `test_max_tokens_over_cap` | 400 `max_tokens_exceeded`，`field=max_tokens`；沒有執行 PII 偵測、沒有 `llm_guardrail` 稽核 |
| `test_max_completion_tokens_over_cap` | 同上，`field=max_completion_tokens` |
| `test_max_tokens_within_cap_kept` | 回傳的 `max_tokens` 為 None（不改寫 client 的值），預留以 client 的值估計 |
| `test_budget_exceeded` | 帳本已有 1.99 → 403 `user_budget_exceeded`，`limit.resets_at`；稽核 `details` 含 `tier`、`limit`、`usage`、`call_id`、`model`；帳本沒有新增 |
| `test_rate_limited` | 429 `user_rate_limited` 與 `retry_after_seconds` |
| `test_injection_deny_leaves_no_reservation` | guardrail 拒絕 → 帳本沒有資料 |
| `test_price_unknown` | 503 `budget_unavailable`，不寫稽核 |
| `test_ledger_unavailable` | 503 `budget_unavailable` |
| `test_duplicate_call_id` | 503 `budget_unavailable` |
| `test_audit_failure_on_denial` | 拒絕時稽核寫入失敗 → 503 `audit_write_failed` |
| `test_settle_and_release_delegate_to_ledger` | `settle()`、`release()` 轉給帳本；帳本例外只記錄類別名稱，不往外丟 |

- [ ] **Step 3: 確認失敗**

- [ ] **Step 4: 實作**

```python
@dataclass(frozen=True)
class LlmRequest:
    key_alias: str | None
    owner: str | None
    user_token: str | None = field(repr=False)
    end_user_override: str | None  # 自帶身份的 header 或欄位名稱
    model: str | None
    call_id: str | None
    messages: list[Message]
    tools: list[Any] | None
    max_tokens: int | None
    max_completion_tokens: int | None


@dataclass
class Admission:
    messages: list[Message]
    pseudonyms: PseudonymMap
    max_tokens: int | None  # client 沒帶上限時要補上的值


class CostEstimator(Protocol):
    def estimate(
        self, model: str, messages: list[Message], tools: list[Any] | None, max_output_tokens: int
    ) -> Estimate: ...


class LlmGate:
    async def admit(self, request: LlmRequest) -> Admission:
        """spec §4.3 的順序；一律 fail-closed，拒絕時寫 llm_access 後丟 GuardrailError。"""
        if request.end_user_override:
            await self._deny(request, None, None, GuardrailError(400, "end_user_override", field=…))
        user = await self._authenticate(request)
        agent, config = await self._guardrail.resolve(request.key_alias, user)
        limits = config.limits or await self._deny(…, GuardrailError(403, "no_llm_quota"))
        output_cap, inject = self._output_cap(request, limits)
        guarded = await self._guardrail.guard_request(agent=agent, config=config, …)
        estimate = self._estimate(request.model, guarded.messages, request.tools, output_cap)
        await self._reserve(request, agent, user, limits, estimate, config.policy_version)
        return Admission(guarded.messages, guarded.pseudonyms, output_cap if inject else None)

    async def settle(self, settlement: Settlement) -> None: ...
    async def release(self, owner: str | None, call_id: object) -> None: ...
```

- token 驗證沿用 `TokenVerifier.verify`，以 `asyncio.to_thread` 執行；`TokenError` → 401 `invalid_user_token`
- `max_tokens`、`max_completion_tokens` 不是正整數時回 400 `max_tokens_exceeded`（與超過上限同一個代碼，`field` 指出是哪一個）
- `expires_at = now + reservation_ttl`；`owner` 或 `call_id` 缺漏時 503 `budget_unavailable`
- 預留的拒絕把 `LimitDenial` 轉成 `GuardrailError(status, code, limit=…)`，稽核 `details` 為 `{"call_id", "model", "tier", "limit", "usage"}`，`limit` 中的上限值從 `limits` 補上（例如 `limit_usd`、`rpm`）
- `settle()`、`release()` 不丟例外：回應已送出或請求已失敗，只能記錯誤日誌（例外類別名稱）

- [ ] **Step 5: 通過並 commit**：`git commit -m "feat: LlmGate admits LLM requests per user"`

---

### Task 9：接上 LiteLLM

**Files:**
- Modify: `src/ai_gov_gateway/plugin.py`、`src/ai_gov_gateway/hook_adapter.py`、`src/ai_gov_gateway/runtime.py`、`src/ai_gov_gateway/config.py`
- Test: `tests/test_hook_adapter.py`、`tests/test_config.py`

- [ ] **Step 1: 結算資料的失敗測試**

```python
def test_settlement_from_success_kwargs():
    kwargs = {
        "litellm_call_id": "c1",
        "standard_logging_object": {
            "response_cost": 0.0123,
            "total_tokens": 456,
            "metadata": {"user_api_key_hash": "hashed-key"},
        },
    }
    settlement = settlement_from_log(kwargs, at=NOW)
    assert settlement == Settlement(
        owner=sha256_hex("hashed-key"), call_id="c1", cost_usd=Decimal("0.0123"), tokens=456, at=NOW
    )


@pytest.mark.parametrize("broken", [{}, {"litellm_call_id": "c1"}, {"litellm_call_id": "c1", "standard_logging_object": {"response_cost": "x"}}])
def test_settlement_missing_fields_returns_none(broken):
    assert settlement_from_log(broken, at=NOW) is None
```

欄位名稱以 Task 1 的 B3 結論為準；B3 顯示 key 雜湊的來源與 `owner_of()` 不同時，兩者改用同一個來源。

- [ ] **Step 2: 實作 hook_adapter 與 plugin**

`LlmGuardrailHook.async_pre_call_hook` 改為：

```python
body = client_body(data)
check_request(body, data)
request = LlmRequest(
    key_alias=getattr(user_api_key_dict, "key_alias", None),
    owner=owner_of(user_api_key_dict),
    user_token=user_token_from_hook(data),
    end_user_override=end_user_header(data) or end_user_field(body),
    model=data.get("model"),
    call_id=call_id,
    messages=data.get("messages") or [],
    tools=data.get("tools"),
    max_tokens=data.get("max_tokens"),
    max_completion_tokens=data.get("max_completion_tokens"),
)
admission = await (await get_services()).llm_gate.admit(request)
```

其後的對照表處理與 M4 相同；`admission.max_tokens` 有值時寫入 `data["max_tokens"]`（B10）。例外處理沿用：`GuardrailError` → `HTTPException(status, detail=error.body())`，其他例外 → 503 `guardrail_unavailable`。

新增：

```python
async def async_log_success_event(self, kwargs, response_obj, start_time, end_time) -> None:
    # B3：streaming 結束與 fallback 成功時各一次；成本以 LiteLLM 的計費為準
    settlement = settlement_from_log(kwargs, at=utc_now())
    if settlement is not None:
        await (await get_services()).llm_gate.settle(settlement)
```

`async_post_call_failure_hook` 在丟棄對照表之後呼叫 `llm_gate.release(owner, call_id)`。

- [ ] **Step 3: runtime 與設定**

- `Settings` 加入 `budget_timezone: str = "Asia/Taipei"`（以 `ZoneInfo` 驗證）、`budget_reservation_ttl_seconds: int = Field(default=600, gt=0)`
- `Services` 加入 `llm_gate`；`build_services()` 以同一個 `PostgresStore` 當帳本與稽核
- `PriceTable` 需要 router：在 `plugin.py` 第一次 pre-call 時從 `litellm.proxy.proxy_server.llm_router` 與 `litellm.model_cost` 建立後注入 gate（router 在 guardrail 載入時可能還沒建好）；建立失敗時 503 `budget_unavailable`
- `tests/test_config.py` 加入兩個設定的預設值與驗證

- [ ] **Step 4: 通過並 commit**：`git commit -m "feat: wire per-user limits into the LiteLLM hooks"`

---

### Task 10：key、測試帳號與既有腳本

**Files:**
- Modify: `scripts/make_keys.py`、`keycloak/realm-governance.json`、`scripts/llm_sim.py`、`scripts/demo_m4.py`、`scripts/rate_limit_check.py`、`tests/integration/test_llm_guardrail_contract.py`、`docker-compose.yml`、`.env.example`

- [ ] **Step 1: key 的總額度**

`KYT_LLM` 改為：

```python
# agent 的總額度，依「使用者數 × 個人月額度」設定；個人額度由 gateway 依 OPA 的等級強制
KYT_LLM = {
    "models": ["kyt-main", "probe-primary", "probe-fallback"],
    "rpm_limit": 60,
    "tpm_limit": 300000,
    "max_budget": 30.0,
    "budget_duration": "1mo",  # B9 不成立時改為 "30d"
}
```

- [ ] **Step 2: 測試帳號**

`realm-governance.json` 加入 realm 角色 `llm_quota_probe` 與使用者 `erin`（只有這個角色，測試密碼比照其他帳號），只供整合測試與 demo 使用。Keycloak 只在 realm 不存在時匯入，改完要重建容器：

```bash
docker compose up -d --force-recreate keycloak
```

- [ ] **Step 3: 既有腳本帶上 token**

`llm_sim.py`、`demo_m4.py`、`rate_limit_check.py` 與 `test_llm_guardrail_contract.py` 的 LLM 請求改為 `x-litellm-api-key: Bearer <key>` 加 `Authorization: Bearer <alice 的 token>`（以 `scripts/kc.py` 既有的 password grant 取得，每個情境前重取，避免 300 秒過期）。`demo_m4` 的 13 個情境預期不變。

- [ ] **Step 4: 驗證並 commit**

```bash
docker compose up -d --build gateway
uv run python -m scripts.make_keys
uv run python -m scripts.demo_m4          # 13/13
uv run pytest -m integration -q          # 既有 32 筆 + Task 5 的帳本測試
git commit -m "chore: user tokens on LLM requests, monthly key budget, quota test user"
```

---

### Task 11：契約測試與 `demo_m5`

**Files:**
- Create: `tests/integration/test_llm_budget_contract.py`、`scripts/demo_m5.py`

- [ ] **Step 1: 契約測試**

需要 gateway、probe_llm、Keycloak。每個測試前清掉 `erin` 在 `governance` 資料庫的 `llm_usage`（`governance` 不是 `_test` 結尾，只刪 `user_sub = erin 的 sub` 的資料，並在測試名稱與 docstring 註明）。

| 測試 | 斷言 |
| ---- | ---- |
| `test_raw_authorization_reaches_hook`（B1） | 帶 token → 200；不帶 → 401 `missing_user_token` |
| `test_jwt_not_forwarded`（B2） | `probe_llm` 記錄的 header 沒有 JWT |
| `test_service_account_token_rejected` | `kyt-agent` client 的 client_credentials token → 401 `invalid_user_token` |
| `test_end_user_overrides_rejected` | body `user`、`metadata.user_id`、兩個 header → 400 `end_user_override` |
| `test_max_tokens_injected`（B10） | 不帶 `max_tokens` → probe 收到等級上限；fallback 也一樣 |
| `test_max_tokens_over_cap` | 400 `max_tokens_exceeded` |
| `test_settlement_matches_spend_logs`（B3） | 非 streaming 與 streaming 各一次：帳本 `cost_usd` 等於 `LiteLLM_SpendLogs.spend`，狀態 `settled` |
| `test_fallback_settles_once`（B3） | primary 503 → fallback：帳本一筆、以 fallback 的成本結算 |
| `test_budget_exhausted_for_one_user_only` | `erin` 用完日額度後 403 `user_budget_exceeded`、`resets_at` 為台北隔日 00:00；`alice` 同時間仍 200；`audit_events` 多一筆 `llm_access` |
| `test_concurrent_requests_stay_within_budget` | `erin` 並行 10 個請求：成功數符合「日額度 ÷ 預留」，帳本本日佔用不超過日額度 |
| `test_rpm_per_user` | `erin` 第 6 個請求 429 `user_rate_limited`，`retry_after_seconds` 在 1–60 |
| `test_rejected_request_releases_reservation` | 以注入 `deny` 的 agent 或 LiteLLM 429 讓請求失敗 → 帳本為 `released` 或沒有資料 |

- [ ] **Step 2: `scripts/demo_m5.py`**

比照 `demo_m4.py`：依序執行情境、每個情境印出名稱與通過與否、最後一行 `N/N 通過`，失敗時結束碼 1。情境與 spec §13 的清單相同（缺 token、無效 token、服務帳號 token、自帶 `user`、自帶 header、`max_tokens` 過大、補上 `max_tokens`、`erin` 用完預算、`alice` 不受影響、並行請求不超過上限、rpm、streaming 結算、fallback 結算）。預期的筆數與金額由腳本從帳本與 probe 的設定算出，不寫死。全部使用 probe，不花 LLM 費用。

- [ ] **Step 3: 執行並 commit**

```bash
uv run pytest -m integration -q
uv run python -m scripts.demo_m5 | tee var/m5-demo.txt
git add tests/integration/test_llm_budget_contract.py scripts/demo_m5.py
git commit -m "test: per-user LLM budget contract tests and demo"
```

---

### Task 12：真實 LLM 驗證（B7、B8）

**Files:**
- Modify: `docs/spikes/m5-findings.md`

- [ ] **Step 1: B7**

以 alice 的 token 對 `kyt-main` 送 2 次 `max_tokens=256` 的請求，要求長篇輸出。從 `LiteLLM_SpendLogs` 讀 `completion_tokens` 與 reasoning token：皆不超過 256 時 B7 成立。成本 < $0.01。

- [ ] **Step 2: B8**

從 kyt_agent 的 `var/audit/` 取 3 個真實案件中最長的 LLM 請求內容（只在本機讀，不寫進 repo），以 alice 的 token 各送一次 `max_tokens=16`，比較請求 JSON 的字元數與回報的 `prompt_tokens`。字元數都比較大時 B8 成立；否則記錄最大比值，回到 Task 6 設定係數。成本 < $0.05。

- [ ] **Step 3: 記錄並 commit**

findings 只記數字與結論，不貼請求內容。`git commit -m "docs: record real LLM checks for M5 estimates"`

---

### Task 13：文件

**Files:**
- Modify（G）：`docs/specs/2026-11-13-user-budget-design.md`、`README.md`、`AGENTS.md`、`docs/images/architecture.svg`
- Modify（workspace repo）：`../AGENTS.md`、`../docs/ai-gov-gateway/`

- [ ] **Step 1: spec 的實作後修訂**

spec 結尾新增「## 16. 實作後修訂」，表格列出與正文不同之處（B1–B10 的結論造成的調整、Task 中改變的錯誤代碼或欄位）。開頭狀態改為「已實作；實作時的調整見 §16」。

- [ ] **Step 2: README**

- 開頭的兩條流量說明：LLM 請求加上「代表哪位使用者、這位使用者的額度夠不夠」
- 設計原則表：新增「依使用者的額度」一列（每位使用者在每個 agent 有自己的日、月預算與 rpm、tpm，在呼叫模型前預留）
- 功能清單：新增依使用者的額度與輸出上限
- 「運作方式」的 mermaid：LLM 流量也帶使用者 token
- LLM guardrail 的「處理順序」改為 spec §4.3 的八步
- 錯誤代碼表加入 spec §10.1 的代碼；稽核事件加入 `llm_access`
- 「fallback 與 tpm」改寫為兩層額度：個人（gateway，事前預留）與 agent 總額（key，事後累計、`budget_duration: "1mo"`）
- 「用量、成本與稽核紀錄」加入 `governance.llm_usage`（以 `call_id` 對照 `LiteLLM_SpendLogs`），`LiteLLM_VerificationToken` 一列補上 `budget_duration`；改寫「probe 沒有定價」為「probe 有測試單價，以 `model_group` 篩掉」
- 環境變數表加入 `GOV_BUDGET_TIMEZONE`、`GOV_BUDGET_RESERVATION_TTL_SECONDS`
- 「快速開始」與「開發」加入 `demo_m5` 與測試數量（實際執行 `uv run pytest -q`、`uv run pytest -m integration -q`、`opa test` 的結果）
- 「已知限制」：LLM 路徑依賴 Postgres；結算事件遺失時低估；rate limit fallback 留下的預留到期前佔用額度；估計偏低時可能少量超過；被注入的 agent 可大量觸發拒絕稽核；移除「LLM 請求因版本未註冊或 agent 已停用被拒絕時不寫稽核」
- 「開發進度」加入 M5；「文件」加入 M5 spec 與 findings

- [ ] **Step 3: AGENTS.md**

- 開發階段表加入 M5：「依委託使用者的 LLM 額度：LLM 路徑驗證使用者 token、拒絕 client 自帶的 end user 身份；依（agent, 使用者）的日月預算、rpm、tpm 與輸出上限，事前預留、回應後結算；key 預算改為每月重置」，狀態「完成」
- 目錄結構加入 `engine/limits.py`、`ledger.py`、`pricing.py`、`llm_gate.py`
- 升級 LiteLLM 的說明：整合測試的數量改為實際數字，加上 `demo_m5`；依賴的 LiteLLM 行為加上 `secret_fields.raw_headers`、`async_log_success_event` 的欄位、auth 先於 guardrail
- 安全段落加入：「LLM 路徑的使用者身份只來自驗證過的 token；`user`、`metadata.user_id`、`x-litellm-end-user-id` 等 client 自帶的身份一律拒絕，不要改成覆寫」、「不要設定 LiteLLM 的 `user_header_mappings`、`user_header_name`、`max_end_user_budget_id`」

- [ ] **Step 4: 架構圖**

`docs/images/architecture.svg`：LLM 流量的能力加入「使用者身份」與「依使用者的額度」，顏色標為 M5，圖例加上 M5。以瀏覽器打開確認文字沒有重疊。

```bash
git add README.md AGENTS.md docs
git commit -m "docs: document M5 per-user LLM limits"
```

- [ ] **Step 5: workspace 的索引與學習文件**

- `../AGENTS.md` 專案索引：ai-gov-gateway 的主題加上「M5 依使用者的 LLM 額度」，狀態改為「M5 完成」
- `../docs/ai-gov-gateway/`：新增一份 M5 的學習文件（為什麼不用 LiteLLM 的 end user 預算、預留與結算、並行與 fallback 的處理、面試問題「某位使用者把 agent 額度用光怎麼辦」的回答），數字引用 `var/m5-demo.txt` 與 findings；`build_html.py` 加入分頁並重建 HTML

```bash
cd .. && uv run --with markdown --with pymdown-extensions python docs/ai-gov-gateway/build_html.py
git add AGENTS.md docs/ai-gov-gateway
git commit -m "docs: add ai-gov-gateway M5 learning notes"
```

---

## Self-Review 對照

| Spec 章節 | 對應 Task |
| --------- | --------- |
| §3 結論 | 全部 |
| §4.1 拒絕自帶的 end user 身份 | 3、8 |
| §4.2 token 的帶法 | 1（B1、B2）、3、9、10 |
| §4.3 處理順序、§4.4 fail-closed | 8、9 |
| §4.5、§12 kyt_agent | 計畫標頭的「上線順序」；kyt_agent 另開計畫 |
| §5 方案比較 | spike 文件（L1–L10） |
| §6 範圍與週期 | 4（等級）、7（日曆期別）、10（key 的 `budget_duration`） |
| §7 rpm、tpm、輸出上限 | 4、7、8 |
| §8.1 帳本、§8.5 並行 | 5 |
| §8.2 估計 | 6、12 |
| §8.3、§8.4 結算、fallback、streaming | 1（B3–B6）、9、11 |
| §9 OPA | 4 |
| §10 錯誤碼與稽核 | 2、7、8 |
| §11 待驗證的假設 | 1、12 |
| §13 測試與驗收 | 4–12 |
| §14 風險 | 11（延遲與並行）、13（已知限制） |
| §15 要更新的文件 | 13 |
