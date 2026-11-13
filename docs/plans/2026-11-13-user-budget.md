# M5 Implementation Plan：依委託使用者限制 LLM 用量（第一階段：事後累計）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** LLM 路徑（`/v1/chat/completions`）驗證委託使用者的 token，並依（agent, 使用者）限制日、月預算、rpm、tpm、同時進行數與單次輸入輸出。已結算的用量達到上限後才拒絕；每一筆超出多少（金額與 token）在結算時精確記錄到帳本，並寫入 hash chain 稽核。

**Architecture:** 新的 `LlmGate`（`engine/llm_gate.py`）串起 M5 的檢查：拒絕 client 自帶的 end user 身份 → 驗證 token → 查 OPA（guardrail 設定與使用者額度）→ 單次上限 → M4 的 guardrail → 在帳本放行並記一筆 `in_flight`。LiteLLM 的 success logging 事件以實際成本結算，結算交易中算出超額並寫稽核；failure hook 釋放。判斷與超額計算是純函式（`engine/limits.py`），交易由 `PostgresStore` 處理（以（agent, 使用者）的 advisory lock 序列化）。結算遺失時以 `ai_gov_gateway.reconcile` 從 `LiteLLM_SpendLogs` 補結算。`engine/` 仍不 import LiteLLM。

**Tech Stack:** LiteLLM v1.105.1、OPA 1.21.1（Rego v1）、Postgres（psycopg）、Keycloak、pytest。

**Spec:** `docs/specs/2026-11-13-user-budget-design.md`（第二階段的預留在 §16，不在本計畫）；靜態檢查與待驗證項目：`docs/spikes/m5-user-budget-spike.md`

**Repo：** `/Users/liumengfu/Desktop/agentic_flow_practice/ai-gov-gateway`（以下簡稱 G），分支 `feat/m5-user-budget`。kyt_agent 的改動另開計畫（spec §12），本計畫不修改 kyt_agent。

**撰寫計畫前已確認的事實**
- LiteLLM 的 end user 預算在 auth 階段、以 client 自填的 id 檢查，guardrail 覆寫 `user` 無效（spike L1–L7），因此採 spec 的方案 B
- `x-litellm-api-key` 在認證時優先於 `Authorization`（L9）；hook 的 `data["secret_fields"]["raw_headers"]` 有原始 header（L8）
- `LlmGuardrail.guard_request()` 目前自己解析 agent 並查 OPA（`engine/guardrails/service.py:68–85`）；`OpaPolicyClient.llm_config(agent)` 只送 `{"agent": …}`（`engine/policy.py:37`）
- `GuardrailConfig` 的欄位缺漏時驗證失敗並視為 `policy_error`（`engine/guardrails/models.py:22`）；`GuardrailError.body()` 只有 `reasons`、`rules`、`field`
- 稽核的 `record_hash` 排除 `None`，新增選填欄位不改變既有紀錄的 hash；`AuditEventType` 是 Literal，新增事件要加進去（`engine/models.py:107`）
- `PostgresStore` 以 `_connect()` 取得連線；`create_approval()` 已示範「資料與稽核在同一個交易」，稽核以 `_append(conn, …)` 在交易內取 `pg_advisory_xact_lock(AUDIT_LOCK_KEY)`（`engine/postgres.py`）
- `owner_of()` 以 `sha256(user_api_key_dict.token)` 識別呼叫者（`hook_adapter.py:47`）
- `fields.py` 的 `REQUEST_FIELDS` 含 `user` 與 `metadata`；`litellm_metadata`、`safety_identifier` 不在白名單
- `LiteLLM_SpendLogs` 有 `litellm_call_id`、`api_key`（雜湊）、`status`、`spend`、`prompt_tokens`、`completion_tokens`；gateway 容器有 `DATABASE_URL`（litellm）與 `GOV_DATABASE_URL`（governance）
- 實測（`LiteLLM_SpendLogs`，195 筆成功的 Gemini 呼叫）：輸入 token 最大 10,919，輸出最大 1,806，單筆成本最大 $0.014
- kyt 的 LLM client 只送 virtual key（kyt_agent `graph/deps.py:_proxy_kwargs`），不送 `user` 與 `max_tokens`；Keycloak 的 `accessTokenLifespan` 為 300 秒
- Keycloak 測試帳號：`alice`（analyst）、`bob`（compliance_supervisor）、`carol`（兩者）
- `litellm.model_cost` 有 `gemini/gemini-3.8-flash` 與 `gpt-6-luna`（不含 `openai/` 前綴）的單價；probe 模型沒有單價（L10）

**Task 1 驗證的假設**（2026-11-13 已完成，結果與對後續 task 的影響見 `docs/spikes/m5-findings.md`；B1–B3、B5、B9、B10 成立，B4、B6 的差異已寫進下方各 task）

| 編號 | 假設 | 用在 |
| ---- | ---- | ---- |
| B1 | hook 的 `secret_fields.raw_headers` 含 client 的 `authorization`（streaming 也是） | Task 3、9 |
| B2 | `x-litellm-api-key` 認證成功，JWT 不被轉送給 provider | Task 3、11 |
| B3 | guardrail 收到 `async_log_success_event`，`litellm_call_id` 與 pre-call 相同，帶實際成本、輸入與輸出 token、key 雜湊；streaming、fallback 各一次 | Task 9 |
| B4 | router 重試的每次失敗都觸發 `async_log_failure_event` | Task 9（不用它釋放） |
| B5 | rate limit fallback 的第二個 `call_id` 收不到事件 | Task 10（對帳標為 released） |
| B6 | streaming 中斷時的事件與成本 | Task 10、14 |
| B9 | key 接受 `budget_duration: "1mo"` | Task 11 |
| B10 | 改寫的 `max_tokens` 會送到 provider，fallback 沿用 | Task 8、9 |

B8（字元數不低於 token 數）只影響 spec §8.6 的上界說明，在 Task 13 以真實請求量測。B7 只有第二階段需要。

**通則**
- 指令在 G 的根目錄執行
- 不輸出 `.env`、`var/keys.json`、API key、密碼或 token 原文
- 每個 task 結束：`uv run pytest -q && uv run ruff format src tests scripts probe_llm && uv run ruff check . && uv run mypy src`
- 動到 `policies/` 時另跑：`docker run --rm -v "$PWD/policies:/policies:ro" openpolicyagent/opa:1.21.1 test /policies`，以及 `fmt --list`、`check --strict`，並更新 `policies/meta/data.json` 的 `policy_version`
- commit 訊息結尾加上 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
- 註解只說明「為什麼」，每段不超過兩行
- 金額一律用 `Decimal`（Postgres `numeric`），不用 float 累加；寫進稽核 `details` 時轉成字串

**上線順序**：M5 合併後，沒有帶使用者 token 的 LLM 請求一律 401。kyt_agent 的 v1.6（spec §12）要在同一次部署切換；在那之前，治理模式的 kyt 只能用 M4 的 gateway。

---

## File Structure

| 檔案 | 職責 |
| ---- | ---- |
| `scripts/spikes/m5_budget_probe.py`、`scripts/spikes/m5_spike.py`、`litellm/config.spike-m5.yaml`、`docker-compose.spike-m5.yml` | Task 1 的驗證程式（另建檔案，保留 M4 的重現環境） |
| `probe_llm/server.py`、`tests/test_probe_llm.py` | Task 1 新增：`GET /request-headers`（header 名稱與是否含 JWT）、`probe:chunk_delay` |
| `docs/spikes/m5-findings.md` | B1–B6、B8–B10 的結論 |
| `src/ai_gov_gateway/engine/models.py` | 事件類型 `llm_access`、`llm_limit_exceeded` |
| `src/ai_gov_gateway/engine/guardrails/models.py` | `GuardrailError` 的 `limit` 欄位；`GuardrailConfig.limits` |
| `src/ai_gov_gateway/engine/limits.py`（新） | `UsageLimits`、`UsageWindows`、`AdmissionSnapshot`、`LimitDenial`、`SettleContext`、`Overage`；期別、放行判斷與超額計算（純函式） |
| `src/ai_gov_gateway/engine/ledger.py`（新） | `UsageLedger` protocol、`Admission`、`Settlement`、`LedgerUnavailable`、`DuplicateCallId` |
| `src/ai_gov_gateway/engine/postgres.py` | `llm_usage` 表；`admit()`、`settle()`、`release()`、`stale_in_flight()` |
| `src/ai_gov_gateway/engine/pricing.py`（新） | `priced_aliases()`：別名與所有 fallback 都有單價的模型 |
| `src/ai_gov_gateway/engine/llm_gate.py`（新） | `LlmGate.admit()`、`settle()`、`release()`；拒絕與超額的稽核事件 |
| `src/ai_gov_gateway/engine/guardrails/service.py` | 拆成 `resolve()`（agent 與 OPA 設定）與 `guard_request()`（M4 的處理） |
| `src/ai_gov_gateway/engine/guardrails/fields.py` | `end_user_field()`：找出 body 的 `user`、`metadata.user_id`、`metadata.user`（由 `LlmGate` 拒絕並寫稽核）；`text_length()` |
| `src/ai_gov_gateway/engine/policy.py` | `llm_config(agent, user)` |
| `src/ai_gov_gateway/hook_adapter.py` | `user_token_from_hook()`、`end_user_header()`、`settlement_from_log()` |
| `src/ai_gov_gateway/plugin.py` | pre-call 改用 `LlmGate`；success logging 結算；failure hook 釋放 |
| `src/ai_gov_gateway/reconcile.py`（新） | 對帳指令：以 `LiteLLM_SpendLogs` 補結算或釋放 |
| `src/ai_gov_gateway/runtime.py`、`config.py` | 建立 `LlmGate`；`GOV_BUDGET_TIMEZONE`、`GOV_USAGE_INFLIGHT_TTL_SECONDS` |
| `policies/governance_llm.rego`、`policies/governance_llm_test.rego`、`policies/agents/data.json`、`policies/meta/data.json` | `llm_limits` 等級、嚴格預設、`limits` 輸出 |
| `litellm/config.yaml` | probe 模型的測試單價（`model_info`） |
| `scripts/make_keys.py` | key 的 `budget_duration`、`max_budget` |
| `keycloak/realm-governance.json` | 測試帳號 `erin`（角色 `llm_quota_probe`） |
| `scripts/llm_sim.py`、`scripts/demo_m4.py`、`scripts/rate_limit_check.py`、`tests/integration/test_llm_guardrail_contract.py` | LLM 請求改為帶使用者 token |
| `scripts/demo_m5.py`（新） | M5 的端到端情境 |
| `tests/test_limits.py`、`tests/test_pricing.py`、`tests/test_llm_gate.py`、`tests/test_reconcile.py`（新）；`tests/test_hook_adapter.py`、`tests/test_guardrail_fields.py`、`tests/test_guardrail_service.py`、`tests/test_policy_client.py`、`tests/test_audit.py`、`tests/test_config.py`、`tests/fakes.py` | 單元測試 |
| `tests/integration/test_usage_ledger.py`（新）、`tests/integration/test_llm_budget_contract.py`（新） | 整合測試 |

---

### Task 1：執行期驗證（B1–B6、B9、B10）

**Files:**
- Create: `scripts/spikes/m5_budget_probe.py`、`scripts/spikes/m5_spike.py`、`litellm/config.spike-m5.yaml`、`docker-compose.spike-m5.yml`、`docs/spikes/m5-findings.md`
- Modify: `probe_llm/server.py`、`tests/test_probe_llm.py`（B2 需要 header 紀錄、B6 需要慢速 streaming）

- [ ] **Step 1: 只記錄的 guardrail**

`scripts/spikes/m5_budget_probe.py`：繼承 `CustomGuardrail`，`mode: [pre_call, post_call]`，在以下方法各寫一行 `M5SPIKE` JSON 日誌（遞增的 `seq`、方法名、`litellm_call_id`）：

| 方法 | 記錄 |
| ---- | ---- |
| `async_pre_call_hook` | `secret_fields` 是否存在、`raw_headers` 的鍵名清單（**只記鍵名**）、是否有 `authorization`、其值是否為三段式 JWT（只記布林）；`data` 的 `max_tokens` |
| `async_log_success_event` | `kwargs["response_cost"]`、`standard_logging_object` 的 `response_cost`、`prompt_tokens`、`completion_tokens`、`total_tokens`、`metadata.user_api_key_hash` 的 sha256 前 8 碼、`model`、`stream` |
| `async_log_failure_event` | `model`、例外類別名稱 |
| `async_post_call_failure_hook` | 例外類別名稱 |
| `async_post_call_success_hook`、`async_post_call_streaming_iterator_hook` | 方法名 |

pre-call 另外把 `data["max_tokens"]` 設為 `17`（B10）。不記錄任何 token 或 key 原文。

- [ ] **Step 2: 驗證腳本**

`scripts/spikes/m5_spike.py` 依序送出（`kyt-agent@1.5` key 放 `x-litellm-api-key`、alice 的 token 放 `Authorization`、model `probe-primary`）：

1. 非 streaming 一次、streaming 一次（B1、B2、B3）
2. `probe_llm` 的請求紀錄：header 沒有 `authorization` 的 JWT（B2）、`max_tokens` 為 17（B10）
3. `PROBE_FAIL_MODELS=probe-primary` 下送一次：主力 503 → fallback（B3 的 fallback、B4、B10 的 fallback）
4. 以 `tpm_limit=1` 的臨時 key 觸發 rate limit fallback（B5），結束後刪除該 key
5. streaming 請求讀到第一個 chunk 後關閉連線（B6）
6. `/key/update` 對臨時 key 設 `budget_duration: "1mo"`，讀回 `budget_reset_at`（B9），結束後刪除
7. 對照 `LiteLLM_SpendLogs`：同一個 `litellm_call_id` 的 `spend`、`prompt_tokens`、`completion_tokens` 與日誌一致（B3），`api_key` 與日誌的 key 雜湊來源一致

- [ ] **Step 3: 執行並記錄**

```bash
docker compose -f docker-compose.yml -f docker-compose.spike.yml up -d --build gateway
uv run python -m scripts.spikes.m5_spike
docker compose logs gateway | grep M5SPIKE > var/m5-spike.log
docker compose up -d --build gateway   # 回到正式設定
```

`docs/spikes/m5-findings.md`：比照 `m4-findings.md`，一個結論表（標明成立與否與證據），加上「對後續 task 的影響」。不貼日誌原文、不貼 header 值。

- [ ] **Step 4: Commit**

```bash
git add scripts/spikes/m5_*.py litellm/config.spike.yaml docker-compose.spike.yml docs/spikes/m5-findings.md
git commit -m "spike: verify LiteLLM hooks for per-user LLM budgets"
```

---

### Task 2：稽核事件與錯誤的 `limit` 欄位

**Files:**
- Modify: `src/ai_gov_gateway/engine/models.py`、`src/ai_gov_gateway/engine/guardrails/models.py`
- Test: `tests/test_audit.py`、`tests/test_guardrail_service.py`

- [ ] **Step 1: 寫失敗的測試**

```python
# tests/test_audit.py
@pytest.mark.parametrize(
    ("event_type", "decision", "details"),
    [
        ("llm_access", "deny", {"call_id": "c1", "tier": "default", "limit": {"type": "budget_day"}}),
        (
            "llm_limit_exceeded",
            "over_limit",
            {"call_id": "c1", "over": {"budget_day": {"limit": "2.0", "before": "1.99", "after": "2.002", "over": "0.002"}}},
        ),
    ],
)
def test_m5_events_verify(event_type, decision, details):
    event = AuditEvent(
        event_type=event_type, agent_id="kyt-agent", user_sub="alice-sub", decision=decision, details=details
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
- `AuditEventType` 加入 `"llm_access"`、`"llm_limit_exceeded"`
- `GuardrailError.__init__` 加入 `limit: dict[str, Any] | None = None`，`body()` 有值時加入 `"limit"`

- [ ] **Step 4: 通過並 commit**：`git commit -m "feat: add M5 audit events and limit error details"`

---

### Task 3：取出使用者 token、偵測自帶的 end user 身份

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
        ({"metadata": '{"user_id": "x"}'}, "metadata.user_id"),  # LiteLLM 也會解析字串形式（spike L2）
        ({"user": ""}, "user"),  # 空字串 LiteLLM 雖然不採用，仍視為自帶身份
    ],
)
def test_end_user_field_detected(body, field):
    assert end_user_field(body) == field


def test_no_end_user_field():
    assert end_user_field({"model": "m", "messages": [], "metadata": {"tag": "x"}}) is None
    assert end_user_field(None) is None


def test_text_length_counts_content_and_tool_arguments():
    messages = [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "t", "type": "function", "function": {"name": "f", "arguments": "{\"a\":1}"}}]},
        {"role": "user", "content": [{"type": "text", "text": "abc"}]},
    ]
    assert text_length(messages) == 2 + 7 + 3
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

`fields.py`：
- `end_user_field(body: Mapping[str, Any] | None) -> str | None`：依序檢查 `user`、`metadata.user_id`、`metadata.user`（`metadata` 是字串時以 JSON 解析）
- `REQUEST_FIELDS` 保留 `user`，加註「由 `LlmGate` 以 `end_user_override` 拒絕並寫稽核」：在 `check_request` 直接拒絕就不會留下稽核
- `text_length(messages) -> int`：訊息文字（含多段內容的文字段）與歷史中 tool call 的參數字元數總和，給 `max_input_chars` 使用；與 M4 `_texts()` 涵蓋相同的欄位，可以重用

- [ ] **Step 4: 通過並 commit**：`git commit -m "feat: read user token from raw headers and detect client end-user ids"`

---

### Task 4：額度等級與 OPA

**Files:**
- Create: `src/ai_gov_gateway/engine/limits.py`（先放 `UsageLimits`）
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
| `test_llm_undefined_when_tier_invalid` | 等級缺 `max_concurrent`、`budget_usd` 不是物件、`budget_usd` 沒有 `day` 也沒有 `month`、有 `week`、數值為負或非整數 → `not llm` |
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
	"max_concurrent": 1,
	"max_input_chars": 50000,
	"max_output_tokens": 2048,
}

limit_counts := {"rpm", "tpm", "max_concurrent", "max_input_chars", "max_output_tokens"}

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
	every name in limit_counts {
		positive_int(tier[name])
	}
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

`policies/agents/data.json` 的 `kyt-agent` 加入 `llm_limits`（順序：測試等級、supervisor、`"*"`；數值依 spec §7 的實測）：

```json
"llm_limits": [
  {"tier": "quota_probe", "role": "llm_quota_probe", "budget_usd": {"day": 0.002, "month": 0.01}, "rpm": 5, "tpm": 5000, "max_concurrent": 3, "max_input_chars": 20000, "max_output_tokens": 64},
  {"tier": "supervisor", "role": "compliance_supervisor", "budget_usd": {"day": 5.0, "month": 50.0}, "rpm": 30, "tpm": 150000, "max_concurrent": 4, "max_input_chars": 100000, "max_output_tokens": 4096},
  {"tier": "default", "role": "*", "budget_usd": {"day": 2.0, "month": 20.0}, "rpm": 20, "tpm": 150000, "max_concurrent": 4, "max_input_chars": 100000, "max_output_tokens": 4096}
]
```

`quota_probe` 只給整合測試與 demo 的 `erin`（Task 11），額度刻意很小。`policy_version` 改為當天日期加序號。

- [ ] **Step 4: Python 模型與 client 的失敗測試**

```python
# tests/test_limits.py
VALID = {
    "tier": "default", "role": "*", "budget_usd": {"day": 2.0}, "rpm": 20, "tpm": 150000,
    "max_concurrent": 4, "max_input_chars": 100000, "max_output_tokens": 4096,
}


def test_limits_parse_decimal_budget():
    assert UsageLimits.model_validate(VALID).budget_usd == {"day": Decimal("2.0")}


def test_limits_reject_unknown_period():
    with pytest.raises(ValidationError):
        UsageLimits.model_validate({**VALID, "budget_usd": {"week": 1}})


# tests/test_policy_client.py
async def test_llm_config_sends_user_and_parses_limits(): ...   # input 含 user；config.limits.tier == "default"
async def test_llm_config_missing_limits_key_is_policy_error(): ...
async def test_llm_config_null_limits_is_allowed(): ...         # config.limits is None
```

- [ ] **Step 5: 實作**

```python
# engine/limits.py
Period = Literal["day", "month"]


class UsageLimits(BaseModel):
    """OPA governance.llm 的 limits；欄位缺漏時驗證失敗，由 policy client 視為 policy_error。"""

    tier: str
    budget_usd: dict[Period, Decimal] = Field(min_length=1)
    rpm: int = Field(gt=0)
    tpm: int = Field(gt=0)
    max_concurrent: int = Field(gt=0)
    max_input_chars: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
```

`GuardrailConfig` 加入 `limits: UsageLimits | None`（**沒有預設值**：OPA 少了這個鍵就是 `policy_error`）。`OpaPolicyClient.llm_config(agent, user)` 送 `{"agent": …, "user": user.model_dump()}`；`GuardrailPolicy` protocol 與 `tests/fakes.py` 的 `FakeGuardrailPolicy`、`guardrail_config()` 同步（fake 記錄最後一次收到的 user，`guardrail_config()` 預設帶一份 `default` 等級）。

- [ ] **Step 6: 通過並 commit**

```bash
git add policies src/ai_gov_gateway/engine tests
git commit -m "feat: per-user LLM limit tiers in OPA"
```

---

### Task 5：放行判斷與超額計算（純函式）

**Files:**
- Modify: `src/ai_gov_gateway/engine/limits.py`
- Test: `tests/test_limits.py`

- [ ] **Step 1: 寫失敗的測試**

```python
TZ = ZoneInfo("Asia/Taipei")
NOW = datetime(2026, 11, 13, 15, 0, tzinfo=UTC)  # 台北 23:00
LIMITS = UsageLimits.model_validate({**VALID, "budget_usd": {"day": 2.0, "month": 20.0}})


def test_windows_use_local_calendar():
    windows = usage_windows(NOW, TZ)
    assert windows.day_start == datetime(2026, 11, 13, 0, 0, tzinfo=TZ)
    assert windows.month_start == datetime(2026, 11, 1, 0, 0, tzinfo=TZ)
    assert windows.minute_start == NOW - timedelta(seconds=60)


def test_window_after_local_midnight():
    windows = usage_windows(datetime(2026, 11, 13, 16, 30, tzinfo=UTC), TZ)  # 台北 11/14 00:30
    assert windows.day_start == datetime(2026, 11, 14, 0, 0, tzinfo=TZ)


# 放行判斷：只看已結算
def test_admit_when_below_limits():
    assert check_admission(snapshot(day_usd="1.99"), LIMITS, NOW, TZ) is None


def test_deny_when_day_budget_reached():
    denial = check_admission(snapshot(day_usd="2.0"), LIMITS, NOW, TZ)
    assert (denial.status, denial.code) == (403, "user_budget_exceeded")
    assert denial.limit == {"type": "budget_day", "resets_at": "2026-11-14T00:00:00+08:00"}
    assert denial.details == {"settled_usd": "2.0", "limit_usd": "2.0"}


def test_month_budget_checked(): ...


def test_concurrency_limit():
    denial = check_admission(snapshot(in_flight=4), LIMITS, NOW, TZ)
    assert (denial.status, denial.code, denial.limit["type"]) == (429, "user_rate_limited", "concurrency")


def test_rpm_limit_returns_retry_after():
    oldest = NOW - timedelta(seconds=48)
    denial = check_admission(snapshot(minute_requests=20, minute_oldest=oldest), LIMITS, NOW, TZ)
    assert denial.limit == {"type": "rpm", "retry_after_seconds": 12}


def test_tpm_counts_settled_tokens_only(): ...


def test_rate_limits_checked_before_budget():
    # 兩者都超過時回 429（可重試），避免短時間大量請求都被記成預算用完
    ...


# 超額計算
def test_overage_zero_below_limit():
    assert overage(before=Decimal("1.0"), cost=Decimal("0.5"), limit=Decimal("2.0")) == Decimal("0")


def test_overage_exactly_reaching_limit_is_zero():
    assert overage(before=Decimal("1.99"), cost=Decimal("0.01"), limit=Decimal("2.0")) == Decimal("0")


def test_overage_of_crossing_request():
    assert overage(before=Decimal("1.99"), cost=Decimal("0.03"), limit=Decimal("2.0")) == Decimal("0.02")


def test_overage_after_limit_is_full_cost():
    assert overage(before=Decimal("2.01"), cost=Decimal("0.03"), limit=Decimal("2.0")) == Decimal("0.03")


@given(st.lists(st.decimals(min_value=0, max_value=1, places=6), min_size=1, max_size=30))
def test_overage_sum_equals_total_excess(costs):
    # 依序結算時，各筆超額加總等於總成本超出上限的量（spec §8.4）
    limit, before, total_over = Decimal("2.0"), Decimal("1.5"), Decimal("0")
    for cost in costs:
        total_over += overage(before=before, cost=cost, limit=limit)
        before += cost
    assert total_over == max(Decimal("0"), before - limit)


def test_settle_context_reports_each_exceeded_item():
    context = SettleContext(
        limits=LIMITS, cost=Decimal("0.03"), tokens=1200,
        day_before=Decimal("1.99"), month_before=Decimal("5"), minute_tokens_before=149_500,
    )
    result = compute_overage(context)
    assert result.day == Decimal("0.02")
    assert result.month == Decimal("0")
    assert result.tpm_tokens == 700
    assert result.exceeded == ["budget_day", "tpm"]
    assert result.crossed == {"budget_day": True, "tpm": True}
```

（property 測試用 `hypothesis`；dev group 沒有時以 `uv add --dev hypothesis` 加入，或改成固定的幾組資料。）

- [ ] **Step 2: 確認失敗**

- [ ] **Step 3: 實作**

```python
class UsageWindows(BaseModel):
    day_start: datetime
    month_start: datetime
    minute_start: datetime


class AdmissionSnapshot(BaseModel):
    """放行時讀出的用量；金額與 token 只含已結算的請求。"""

    day_usd: Decimal
    month_usd: Decimal
    in_flight: int
    minute_requests: int
    minute_oldest: datetime | None
    minute_tokens: int
    minute_oldest_settled: datetime | None


class LimitDenial(BaseModel):
    status: int
    code: str
    limit: dict[str, Any]
    details: dict[str, Any]


def usage_windows(now: datetime, tz: ZoneInfo) -> UsageWindows: ...


def check_admission(
    snapshot: AdmissionSnapshot, limits: UsageLimits, now: datetime, tz: ZoneInfo
) -> LimitDenial | None:
    """依序檢查同時進行數、rpm、tpm、日、月；已結算達到上限才拒絕。"""


def overage(*, before: Decimal, cost: Decimal, limit: Decimal) -> Decimal:
    """這一筆超出上限的部分：只算 limit 以上、且在 before 之後的成本。"""
    return max(Decimal("0"), before + cost - max(before, limit))


class SettleContext(BaseModel):
    limits: UsageLimits
    cost: Decimal
    tokens: int
    day_before: Decimal
    month_before: Decimal
    minute_tokens_before: int


class Overage(BaseModel):
    day: Decimal
    month: Decimal
    tpm_tokens: int
    exceeded: list[str]
    crossed: dict[str, bool]
    breakdown: dict[str, dict[str, str]]  # 稽核的 details.over


def compute_overage(context: SettleContext) -> Overage: ...
```

- `resets_at` 以 `tz` 的下一個日或月起點，ISO 格式含時區
- `retry_after_seconds = max(1, ceil(oldest + 60s − now))`；rpm 用 `minute_oldest`，tpm 用 `minute_oldest_settled`；同時進行數固定回 1
- 期別沒有設定（例如只有 `day`）時不檢查也不計算該項
- 金額在 `details` 與 `breakdown` 中轉成字串

- [ ] **Step 4: 通過並 commit**：`git commit -m "feat: admission checks and overage calculation for per-user limits"`

---

### Task 6：用量帳本

**Files:**
- Create: `src/ai_gov_gateway/engine/ledger.py`、`tests/integration/test_usage_ledger.py`
- Modify: `src/ai_gov_gateway/engine/postgres.py`、`tests/fakes.py`

- [ ] **Step 1: 介面**

```python
# engine/ledger.py
class LedgerUnavailable(Exception):
    """帳本無法讀寫；pre-call 一律拒絕（budget_unavailable）。"""


class DuplicateCallId(Exception):
    """同一把 key 重複使用 call_id；call_id 可由 client 以 x-litellm-call-id 指定。"""


class Admission(BaseModel):
    owner: str
    call_id: str
    agent_id: str
    user_sub: str
    model: str
    limits: UsageLimits
    windows: UsageWindows
    inflight_since: datetime   # now − GOV_USAGE_INFLIGHT_TTL_SECONDS
    now: datetime


class Settlement(BaseModel):
    owner: str
    call_id: str
    cost_usd: Decimal
    prompt_tokens: int
    completion_tokens: int
    at: datetime
    settled_by: Literal["callback", "reconcile"]


class SettledRow(BaseModel):
    """結算後的帳本列；給稽核事件使用。"""

    agent_id: str
    user_sub: str
    model: str
    tier: str
    call_id: str
    created_at: datetime


AuditBuilder = Callable[[SettledRow, Overage, Settlement], AuditEvent]


class UsageLedger(Protocol):
    async def admit(
        self, admission: Admission, check: Callable[[AdmissionSnapshot], LimitDenial | None]
    ) -> LimitDenial | None:
        """在同一個交易中讀出用量、呼叫 check；沒有拒絕才寫入 in_flight。"""

    async def settle(
        self, settlement: Settlement, tz: ZoneInfo, build_audit: AuditBuilder
    ) -> Overage | None:
        """計算超額、更新帳本；有超額時同一個交易寫入稽核。找不到或已結算時回傳 None。"""

    async def release(self, owner: str, call_id: str) -> bool: ...

    async def stale_in_flight(self, older_than: datetime, limit: int) -> list[tuple[str, str]]:
        """給對帳使用：(owner, call_id)。"""
```

`check` 與超額計算都是 Task 5 的純函式，交易只負責一致性。

- [ ] **Step 2: 整合測試（先寫）**

`tests/integration/test_usage_ledger.py`（`governance_test` 資料庫，沿用 `test_postgres_store.py` 的 fixture）：

| 測試 | 內容 |
| ---- | ---- |
| `test_admit_inserts_in_flight` | check 回 None → 寫入一筆 `in_flight`，`limits` 快照正確 |
| `test_admit_skips_insert_when_denied` | check 回 `LimitDenial` → 回傳該拒絕、表中沒有新資料 |
| `test_snapshot_counts_only_settled_cost` | 一筆已結算（0.01）、一筆 `in_flight`、一筆 `released` → `day_usd == 0.01`、`in_flight == 1` |
| `test_snapshot_in_flight_ignores_stale` | 放行超過 TTL 的 `in_flight` 不計入同時進行數 |
| `test_snapshot_separates_day_and_month` | 上個月、本月較早、今天各一筆 → day 只含今天，month 含本月 |
| `test_snapshot_minute_windows` | rpm 以 `created_at`、tpm 以 `settled_at` 計算 60 秒視窗 |
| `test_minute_window_across_month_start` | 每月 1 日 00:00:30，前一個月 23:59:50 的請求仍算進 60 秒視窗 |
| `test_settle_records_cost_tokens_and_zero_overage` | 未超額 → 三個超額欄位為 0，沒有稽核 |
| `test_settle_crossing_writes_audit` | 已結算 1.99、這一筆 0.03 → `over_day_usd == 0.02`；`audit_events` 多一筆 `llm_limit_exceeded`，`details.crossed.budget_day` 為 true；hash chain 驗證通過 |
| `test_settle_uses_admission_snapshot_limits` | 放行後改變等級的上限，結算仍以快照計算 |
| `test_settle_after_release_still_records` | 釋放後才收到結算 → 狀態 `settled`、成本寫入 |
| `test_settle_twice_is_noop` | 已結算的再結算一次 → 回傳 None，數值不變 |
| `test_release_does_not_override_settled` | 結算後釋放 → 仍是 `settled` |
| `test_concurrent_settlements_sum_exactly` | 已結算 1.90，20 筆 `in_flight` 以 `asyncio.gather` 同時結算（各 0.01–0.03 的隨機值，固定 seed）→ `sum(over_day_usd) == max(0, 總成本 − 2.0)`；`over > 0` 的筆數等於 `llm_limit_exceeded` 稽核數 |
| `test_audit_failure_rolls_back_settlement` | `build_audit` 丟例外（或稽核寫入失敗）→ 帳本仍是 `in_flight`，回傳例外為 `LedgerUnavailable` |
| `test_different_users_do_not_block` | 一位使用者的交易持有鎖時，另一位使用者的放行能完成（以 `asyncio.wait_for` 檢查） |
| `test_duplicate_call_id_raises` | 同一 owner 重複 call_id → `DuplicateCallId`；不同 owner 可重複 |
| `test_stale_in_flight_lists_old_rows` | 只列出指定時間之前放行、仍為 `in_flight` 的列 |
| `test_numeric_precision` | `Decimal("0.000000375")` 寫入讀回不變 |
| `test_unavailable_database_raises` | 錯誤的 URL → `LedgerUnavailable` |

- [ ] **Step 3: 確認失敗**：`uv run pytest -m integration tests/integration/test_usage_ledger.py -q`

- [ ] **Step 4: 實作**

`SCHEMA` 加入 spec §8.1 的 `llm_usage` 表與兩個索引。`PostgresStore` 加上：

```python
# 與 AUDIT_LOCK_KEY 不同的命名空間：雙參數的 advisory lock，第二個參數是（agent, 使用者）
USAGE_LOCK_SPACE = 72_410_002

ADMISSION_SNAPSHOT = """
SELECT
  coalesce(sum(cost_usd) FILTER (WHERE status = 'settled' AND created_at >= %(day_start)s), 0) AS day_usd,
  coalesce(sum(cost_usd) FILTER (WHERE status = 'settled' AND created_at >= %(month_start)s), 0) AS month_usd,
  count(*) FILTER (WHERE status = 'in_flight' AND created_at > %(inflight_since)s) AS in_flight,
  count(*) FILTER (WHERE created_at > %(minute_start)s) AS minute_requests,
  min(created_at) FILTER (WHERE created_at > %(minute_start)s) AS minute_oldest,
  coalesce(sum(prompt_tokens + completion_tokens)
           FILTER (WHERE status = 'settled' AND settled_at > %(minute_start)s), 0) AS minute_tokens,
  min(settled_at) FILTER (WHERE status = 'settled' AND settled_at > %(minute_start)s) AS minute_oldest_settled
FROM llm_usage
WHERE agent_id = %(agent_id)s AND user_sub = %(user_sub)s
  AND (created_at >= least(%(month_start)s, %(minute_start)s, %(inflight_since)s)
       OR settled_at > %(minute_start)s)
"""


async def _lock_user(conn, agent_id: str, user_sub: str) -> None:
    await conn.execute(
        "SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (USAGE_LOCK_SPACE, f"{agent_id}:{user_sub}")
    )
```

`settle()` 的交易：

1. `SELECT agent_id, user_sub, … FROM llm_usage WHERE owner AND call_id`（不加鎖），找不到或已 `settled` → 回傳 None
2. `_lock_user()`，再以 `FOR UPDATE` 重讀這一列並確認不是 `settled`（避免兩個結算事件同時進來）
3. 以這一列的 `created_at` 與 `tz` 算出期別（`usage_windows`），讀出**其他**已結算請求的 `day_before`、`month_before`，以及結算時間往前 60 秒的 `minute_tokens_before`
4. `compute_overage()`；更新 `status = 'settled'`、成本、token、三個超額欄位、`settled_at`、`settled_by`
5. `overage.exceeded` 不為空時，以 `build_audit(row, overage, settlement)` 建立事件並呼叫 `_append(conn, event, settlement.at)`（取稽核鎖；鎖的順序固定為使用者鎖 → 稽核鎖）

`release()`：`UPDATE … SET status = 'released' WHERE owner AND call_id AND status = 'in_flight'`。

連線與一般錯誤沿用 `_connect()` 的轉換，但帳本的呼叫端只認 `LedgerUnavailable`：在方法外層把 `StoreUnavailable`、`StoreError` 轉成 `LedgerUnavailable`。

`tests/fakes.py` 加入 `InMemoryLedger`（以 `asyncio.Lock` 模擬序列化，snapshot 與超額規則與 SQL 相同，稽核寫進傳入的 `InMemoryStore`），供 Task 8 的單元測試使用。

- [ ] **Step 5: 通過並 commit**：`git commit -m "feat: usage ledger with overage records in the governance database"`

---

### Task 7：單價檢查

**Files:**
- Create: `src/ai_gov_gateway/engine/pricing.py`、`tests/test_pricing.py`
- Modify: `litellm/config.yaml`

沒有單價時 LiteLLM 算出的成本是 0，事後累計的預算形同虛設，所以放行前要確認請求的模型別名與它所有的 fallback 都有單價。

- [ ] **Step 1: 寫失敗的測試**

```python
MODEL_LIST = [
    {"model_name": "kyt-main", "litellm_params": {"model": "gemini/gemini-3.8-flash"}},
    {"model_name": "kyt-main-fallback", "litellm_params": {"model": "openai/gpt-6-luna"}},
    {"model_name": "probe-primary", "litellm_params": {"model": "openai/probe-primary"},
     "model_info": {"input_cost_per_token": 1e-6, "output_cost_per_token": 1e-5}},
    {"model_name": "unpriced", "litellm_params": {"model": "openai/nope"}},
]
COSTS = {
    "gemini/gemini-3.8-flash": {"input_cost_per_token": 7.5e-7, "output_cost_per_token": 3.75e-6},
    "gpt-6-luna": {"input_cost_per_token": 1e-7, "output_cost_per_token": 5e-7},
}


def test_alias_and_fallbacks_priced():
    priced = priced_aliases(MODEL_LIST, [{"kyt-main": ["kyt-main-fallback"]}], COSTS)
    assert {"kyt-main", "kyt-main-fallback", "probe-primary"} <= priced


def test_provider_prefix_is_stripped_when_not_found():
    assert "kyt-main-fallback" in priced_aliases(MODEL_LIST, [], COSTS)


def test_model_info_counts_as_price():
    assert "probe-primary" in priced_aliases(MODEL_LIST, [], {})


def test_unpriced_alias_excluded():
    assert "unpriced" not in priced_aliases(MODEL_LIST, [], COSTS)


def test_unpriced_fallback_excludes_alias():
    # fallback 沒有單價時那次呼叫的成本為 0，整個別名視為沒有單價
    assert "kyt-main" not in priced_aliases(MODEL_LIST, [{"kyt-main": ["unpriced"]}], COSTS)


def test_fallback_cycle_terminates(): ...
```

- [ ] **Step 2: 確認失敗**

- [ ] **Step 3: 實作**

- `priced_aliases(model_list, fallbacks, model_cost) -> frozenset[str]`：一個別名的所有 deployment 都有輸入與輸出單價（先看 `model_info`，再查 `model_cost[model]`，找不到時去掉 `<provider>/` 前綴再查），且 fallback 鏈（遞迴、防循環）上的別名也都符合，才列入
- 只收 plain data，不 import LiteLLM；plugin 在 Task 9 從 `llm_router.model_list`、`llm_router.fallbacks`、`litellm.model_cost` 建立

`litellm/config.yaml` 的 probe 模型加上測試單價（讓整合測試與 demo 能用到預算）：

```yaml
  - model_name: probe-primary
    litellm_params: …
    # 測試單價：讓依使用者的預算在不花 LLM 費用的情況下也能驗證
    model_info: {input_cost_per_token: 0.000001, output_cost_per_token: 0.00001}
```

`probe-fallback` 同樣設定。README 中「probe 沒有定價」的說明在 Task 14 改寫。

- [ ] **Step 4: 通過並 commit**：`git commit -m "feat: require prices for governed model aliases"`

---

### Task 8：`LlmGate`

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

`tests/test_llm_gate.py` 以 `FakeTokenVerifier`（測試內定義：固定 token 對應 `UserIdentity`，其他丟 `TokenError`）、`FakeGuardrailPolicy`、`InMemoryStore`、`InMemoryLedger`、`FakePiiDetector`、`FakeClock` 組出 gate，`priced` 預設含測試模型：

| 測試 | 預期 |
| ---- | ---- |
| `test_admit_happy_path` | 回傳改寫後的訊息、`max_tokens` 補為等級上限、帳本一筆 `in_flight`、沒有 `llm_access` 稽核 |
| `test_end_user_override_denied_before_token` | header 與 body 欄位各一例：400 `end_user_override`，`field` 為 header 或欄位名稱；稽核 `llm_access`，`user_sub` 為 None；沒有驗證 token |
| `test_missing_token` | 401 `missing_user_token`；稽核有 agent 沒有 user；沒有查 OPA |
| `test_invalid_token` | 401 `invalid_user_token`；稽核 |
| `test_no_matching_tier` | `limits=None` → 403 `no_llm_quota`；稽核含 `policy_version` |
| `test_max_tokens_over_cap` | 400 `max_tokens_exceeded`，`field=max_tokens`；沒有執行 PII 偵測、沒有 `llm_guardrail` 稽核 |
| `test_max_completion_tokens_over_cap` | 同上，`field=max_completion_tokens` |
| `test_max_tokens_not_positive_int` | `0`、`-1`、`1.5`、`"10"` → 400 `max_tokens_exceeded` |
| `test_max_tokens_within_cap_kept` | 回傳的 `max_tokens` 為 None（不改寫 client 的值） |
| `test_input_over_tier_limit` | 文字總長超過 `max_input_chars` → 400 `content_too_large`；稽核 `llm_access` |
| `test_budget_reached` | 帳本已結算 2.0 → 403 `user_budget_exceeded`，`limit.resets_at`；稽核 `details` 含 `tier`、`limit`、`settled_usd`、`call_id`、`model`；帳本沒有新增 |
| `test_rate_limited` | 429 `user_rate_limited` 與 `retry_after_seconds` |
| `test_concurrency_limited` | 4 筆 `in_flight` → 第 5 筆 429，`limit.type == "concurrency"` |
| `test_injection_deny_leaves_no_row` | guardrail 拒絕 → 帳本沒有資料 |
| `test_unpriced_model` | 503 `budget_unavailable`，不寫稽核 |
| `test_ledger_unavailable` | 503 `budget_unavailable` |
| `test_duplicate_call_id` | 503 `budget_unavailable` |
| `test_audit_failure_on_denial` | 拒絕時稽核寫入失敗 → 503 `audit_write_failed` |
| `test_settle_crossing_writes_limit_exceeded` | 已結算 1.99，結算 0.03 → 帳本 `over_day_usd == 0.02`，稽核 `llm_limit_exceeded` 的 `reasons == ["budget_day"]`、`details.over.budget_day` 正確、`details.settled_by == "callback"` |
| `test_settle_and_release_never_raise` | 帳本例外只記錄類別名稱，不往外丟 |

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
    max_tokens: object
    max_completion_tokens: object


@dataclass
class Admitted:
    messages: list[Message]
    pseudonyms: PseudonymMap
    max_tokens: int | None  # client 沒帶上限時要補上的值


class LlmGate:
    async def admit(self, request: LlmRequest) -> Admitted:
        """spec §4.3 的順序；一律 fail-closed，拒絕時寫 llm_access 後丟 GuardrailError。"""
        if request.end_user_override:
            await self._deny(request, None, None, GuardrailError(400, "end_user_override", field=…))
        user = await self._authenticate(request)
        agent, config = await self._guardrail.resolve(request.key_alias, user)
        limits = config.limits or await self._deny(…, GuardrailError(403, "no_llm_quota"))
        inject = self._check_single_request(request, limits)   # 輸出上限與 max_input_chars
        guarded = await self._guardrail.guard_request(agent=agent, config=config, …)
        self._require_price(request.model)
        await self._admit_usage(request, agent, user, limits, config.policy_version)
        return Admitted(guarded.messages, guarded.pseudonyms, limits.max_output_tokens if inject else None)

    async def settle(self, settlement: Settlement) -> None: ...
    async def release(self, owner: str | None, call_id: object) -> None: ...
```

- token 驗證沿用 `TokenVerifier.verify`，以 `asyncio.to_thread` 執行；`TokenError` → 401 `invalid_user_token`
- `priced` 由 runtime 注入（`Callable[[], frozenset[str]]`，第一次使用時才建立，見 Task 9）；取不到時 503 `budget_unavailable`
- `owner` 或 `call_id` 缺漏時 503 `budget_unavailable`
- 放行的拒絕把 `LimitDenial` 轉成 `GuardrailError(status, code, limit=…)`，稽核 `details` 為 `{"call_id", "model", "tier", "limit", …denial.details}`
- `settle()` 傳給帳本的 `build_audit` 建立 `llm_limit_exceeded`：`decision="over_limit"`、`reasons=overage.exceeded`、`details` 依 spec §10.2（`call_id`、`model`、`tier`、`cost_usd`、`tokens: {"prompt", "completion"}`、`over`、`crossed`、`settled_by`）
- `settle()`、`release()` 不丟例外：回應已送出或請求已失敗，只能記錯誤日誌（例外類別名稱），遺失的結算由對帳補上

- [ ] **Step 5: 通過並 commit**：`git commit -m "feat: LlmGate admits and settles LLM requests per user"`

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
            "prompt_tokens": 400,
            "completion_tokens": 56,
            "metadata": {"user_api_key_hash": "hashed-key"},
        },
    }
    assert settlement_from_log(kwargs, at=NOW) == Settlement(
        owner=sha256_hex("hashed-key"), call_id="c1", cost_usd=Decimal("0.0123"),
        prompt_tokens=400, completion_tokens=56, at=NOW, settled_by="callback",
    )


@pytest.mark.parametrize(
    "broken",
    [{}, {"litellm_call_id": "c1"}, {"litellm_call_id": "c1", "standard_logging_object": {"response_cost": "x"}}],
)
def test_settlement_missing_fields_returns_none(broken):
    assert settlement_from_log(broken, at=NOW) is None
```

依 B3：call_id 取 `kwargs["litellm_call_id"]`（`standard_logging_object["id"]` 不是 call_id）；`metadata.user_api_key_hash` 與 `user_api_key_dict.token` 同值，`owner` 與 `owner_of()` 一致。成本是有二進位誤差的 float（`0.00012200000000000001`），以 `Decimal(str(round(value, 12)))` 轉換，才會等於 `SpendLogs.spend`；測試加一例 `response_cost=0.00012200000000000001` → `Decimal("0.000122")`。

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
    max_tokens=data.get("max_tokens"),
    max_completion_tokens=data.get("max_completion_tokens"),
)
admitted = await (await get_services()).llm_gate.admit(request)
```

其後的對照表處理與 M4 相同；`admitted.max_tokens` 有值時寫入 `data["max_tokens"]`（B10）。例外處理沿用：`GuardrailError` → `HTTPException(status, detail=error.body())`，其他例外 → 503 `guardrail_unavailable`。

新增：

```python
async def async_log_success_event(self, kwargs, response_obj, start_time, end_time) -> None:
    # B3：streaming 結束與 fallback 成功時各一次；成本以 LiteLLM 的計費為準
    settlement = settlement_from_log(kwargs, at=utc_now())
    if settlement is None:
        logger.error("llm usage settlement skipped: missing fields")
        return
    await (await get_services()).llm_gate.settle(settlement)
```

`async_post_call_failure_hook` 在丟棄對照表之後呼叫 `llm_gate.release(owner, call_id)`。

- [ ] **Step 3: runtime 與設定**

- `Settings` 加入 `budget_timezone: str = "Asia/Taipei"`（以 `ZoneInfo` 驗證）、`usage_inflight_ttl_seconds: int = Field(default=300, gt=0)`
- `Services` 加入 `llm_gate`；`build_services()` 以同一個 `PostgresStore` 當帳本與稽核
- `priced_aliases` 需要 router：`plugin.py` 提供一個 lazy 函式，第一次呼叫時從 `litellm.proxy.proxy_server.llm_router` 與 `litellm.model_cost` 建立並快取（router 在 guardrail 載入時可能還沒建好）；建立失敗時下次重試，該次 503 `budget_unavailable`
- `tests/test_config.py` 加入兩個設定的預設值與驗證

- [ ] **Step 4: 通過並 commit**：`git commit -m "feat: wire per-user limits into the LiteLLM hooks"`

---

### Task 10：對帳

**Files:**
- Create: `src/ai_gov_gateway/reconcile.py`、`tests/test_reconcile.py`

- [ ] **Step 1: 寫失敗的測試**

`tests/test_reconcile.py` 以 `InMemoryLedger` 與假的 SpendLogs 讀取函式測試：

| 測試 | 預期 |
| ---- | ---- |
| `test_success_log_settles_with_reconcile_tag` | SpendLogs 有 `success` → 以其 `spend`、token 結算，`settled_by == "reconcile"`，超額照常計算並寫稽核 |
| `test_failure_only_releases` | 只有失敗紀錄 → `released` |
| `test_missing_log_kept_before_threshold` | 沒有紀錄、放行未超過 `release_missing_after` → 保持 `in_flight`，`unresolved` 加一 |
| `test_missing_log_released_after_threshold` | 沒有紀錄、放行超過 `release_missing_after` → `released`，`released_missing` 加一（B5：rate limit fallback 的第二個 call_id 不會有 SpendLogs） |
| `test_owner_matches_hashed_api_key` | SpendLogs 的 `api_key` 經 `sha256_hex` 後才與 `owner` 比對 |
| `test_output_has_no_user_identifiers` | 輸出只含數量與 `call_id` |

- [ ] **Step 2: 實作**

```python
async def reconcile(
    ledger: UsageLedger,
    spend_logs: SpendLogReader,
    gate: LlmGate,
    *,
    older_than: datetime,
    release_missing_before: datetime,
    batch: int = 500,
) -> ReconcileReport:
    """把逾時的 in_flight 依 LiteLLM_SpendLogs 結算或釋放；結算走與 callback 相同的超額計算。"""
```

- `SpendLogReader` 以 psycopg 讀 litellm 資料庫：`SELECT status, spend, prompt_tokens, completion_tokens, api_key FROM "LiteLLM_SpendLogs" WHERE litellm_call_id = %s`，在程式中比對 `sha256_hex(api_key) == owner`（B3：`api_key` 欄位就是 key 的雜湊，與 `user_api_key_dict.token` 同值；`owner` 是它的 sha256，不能反查）
- `__main__`：`python -m ai_gov_gateway.reconcile --older-than 10m --release-missing-after 1h`，從 `GOV_DATABASE_URL` 與 `DATABASE_URL` 連線（gateway 容器內都有），輸出 `settled=N released=N released_missing=N unresolved=N`
- 不 import LiteLLM

- [ ] **Step 3: 通過並 commit**：`git commit -m "feat: reconcile in-flight LLM usage from LiteLLM spend logs"`

---

### Task 11：key、測試帳號與既有腳本

**Files:**
- Modify: `scripts/make_keys.py`、`keycloak/realm-governance.json`、`scripts/llm_sim.py`、`scripts/demo_m4.py`、`scripts/rate_limit_check.py`、`tests/integration/test_llm_guardrail_contract.py`、`docker-compose.yml`、`.env.example`

- [ ] **Step 1: key 的總額度**

```python
# agent 的總額度，依「使用者數 × 個人月額度」設定；個人額度由 gateway 依 OPA 的等級強制
KYT_LLM = {
    "models": ["kyt-main", "probe-primary", "probe-fallback"],
    "rpm_limit": 60,
    "tpm_limit": 300000,
    "max_budget": 30.0,
    "budget_duration": "1mo",  # B9：每月 1 日 00:00 UTC 重置
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
uv run pytest -m integration -q          # 既有 32 筆 + Task 6 的帳本測試
git commit -m "chore: user tokens on LLM requests, monthly key budget, quota test user"
```

---

### Task 12：契約測試與 `demo_m5`

**Files:**
- Create: `tests/integration/test_llm_budget_contract.py`、`scripts/demo_m5.py`

- [ ] **Step 1: 契約測試**

需要 gateway、probe_llm、Keycloak。每個測試前清掉 `erin` 在 `governance` 資料庫的 `llm_usage`（`governance` 不是 `_test` 結尾，只刪 `user_sub = erin 的 sub` 的資料，並在 docstring 註明）。成本以 probe 的 `probe:usage=` 指令控制，預期值由測試單價算出。

| 測試 | 斷言 |
| ---- | ---- |
| `test_raw_authorization_reaches_hook`（B1） | 帶 token → 200；不帶 → 401 `missing_user_token` |
| `test_jwt_not_forwarded`（B2） | `probe_llm` 記錄的 header 沒有 JWT |
| `test_service_account_token_rejected` | `kyt-agent` client 的 client_credentials token → 401 `invalid_user_token` |
| `test_end_user_overrides_rejected` | body `user`、`metadata.user_id`、兩個 header → 400 `end_user_override`；`LiteLLM_SpendLogs` 沒有這些請求 |
| `test_max_tokens_injected`（B10） | 不帶 `max_tokens` → probe 收到等級上限；fallback 也一樣 |
| `test_single_request_caps` | `max_tokens` 過大 → 400 `max_tokens_exceeded`；輸入超過 `max_input_chars` → 400 `content_too_large` |
| `test_settlement_matches_spend_logs`（B3） | 非 streaming 與 streaming 各一次：帳本的成本與 token 等於 `LiteLLM_SpendLogs`，`settled_by == "callback"` |
| `test_fallback_settles_once`（B3） | primary 503 → fallback：帳本一筆、以 fallback 的成本結算 |
| `test_crossing_request_records_overage` | `erin` 已結算接近日額度，再送一筆跨過上限 → 200；帳本 `over_day_usd` 等於「之後總額 − 日額度」；`llm_limit_exceeded` 一筆、`crossed` 為 true；下一筆 403 `user_budget_exceeded`，`resets_at` 為台北隔日 00:00 |
| `test_concurrent_crossing_overage_sums_exactly` | `erin` 接近日額度時並行 3 筆（`max_concurrent` 3）：全數 200；`sum(over_day_usd) == 期別總成本 − 日額度`；`over > 0` 的筆數等於 `llm_limit_exceeded` 數 |
| `test_other_user_unaffected` | `erin` 被擋時 `alice` 仍 200 |
| `test_rpm_per_user` | `erin` 第 6 個請求 429 `user_rate_limited`，`retry_after_seconds` 在 1–60 |
| `test_rejected_request_releases_row` | LiteLLM 的 key 限流擋下（或 provider 錯誤且無 fallback）→ 帳本為 `released` |
| `test_reconcile_settles_missing_callback` | 直接把一筆已結算的列改回 `in_flight`、清空成本，執行 `docker compose exec gateway python -m ai_gov_gateway.reconcile --older-than 0s` → 成本與 SpendLogs 一致，`settled_by == "reconcile"` |

- [ ] **Step 2: `scripts/demo_m5.py`**

比照 `demo_m4.py`：依序執行情境、每個情境印出名稱與通過與否、最後一行 `N/N 通過`，失敗時結束碼 1。情境與 spec §13 的清單相同。跨過上限的情境另外印出帳本中每一筆的 `cost_usd`、`over_day_usd` 與 `crossed`，讓人看得出超額怎麼分配。全部使用 probe，不花 LLM 費用。

- [ ] **Step 3: 執行並 commit**

```bash
uv run pytest -m integration -q
uv run python -m scripts.demo_m5 | tee var/m5-demo.txt
git add tests/integration/test_llm_budget_contract.py scripts/demo_m5.py
git commit -m "test: per-user LLM budget contract tests and demo"
```

---

### Task 13：真實 LLM 驗證

**Files:**
- Modify: `docs/spikes/m5-findings.md`

- [ ] **Step 1: 真實單價的結算**

以 alice 的 token 對 `kyt-main` 送 2 次 `max_tokens=256` 的請求：帳本的成本與 token 等於 `LiteLLM_SpendLogs`，成本大於 0。成本 < $0.01。

- [ ] **Step 2: B8（只影響上界說明）**

從 kyt_agent 的 `var/audit/` 取 3 個真實案件中最長的 LLM 請求內容（只在本機讀，不寫進 repo），以 alice 的 token 各送一次 `max_tokens=16`，比較文字的字元數與回報的 `prompt_tokens`。記錄最大比值；字元數比較小時，spec §8.6 的上界乘上這個比值。成本 < $0.05。

- [ ] **Step 3: 記錄並 commit**

findings 只記數字與結論，不貼請求內容。`git commit -m "docs: record real LLM checks for M5"`

---

### Task 14：文件

**Files:**
- Modify（G）：`docs/specs/2026-11-13-user-budget-design.md`、`README.md`、`AGENTS.md`、`docs/images/architecture.svg`
- Modify（workspace repo）：`../AGENTS.md`、`../docs/ai-gov-gateway/`

- [ ] **Step 1: spec 的實作後修訂**

spec 的 §16 之後新增「## 17. 實作後修訂」，表格列出與正文不同之處（B1–B10 的結論造成的調整、Task 中改變的錯誤代碼或欄位、B8 的實測比值與修正後的超額上界）。開頭狀態改為「第一階段已實作；實作時的調整見 §17」。

- [ ] **Step 2: README**

- 開頭的兩條流量說明：LLM 請求加上「代表哪位使用者、這位使用者的額度還夠不夠」
- 設計原則表：新增「依使用者的額度」一列（每位使用者在每個 agent 有自己的日、月預算與流量上限；用完後只擋他自己，超出的量逐筆記錄）
- 功能清單：新增依使用者的額度、單次上限、超額紀錄與對帳
- 「運作方式」的 mermaid：LLM 流量也帶使用者 token
- LLM guardrail 的「處理順序」改為 spec §4.3 的八步
- 錯誤代碼表加入 spec §10.1 的代碼；稽核事件加入 `llm_access` 與 `llm_limit_exceeded`
- 「fallback 與 tpm」改寫為兩層額度：個人（gateway，事後累計、超額有上界並逐筆記錄）與 agent 總額（key，事後累計、`budget_duration: "1mo"`）
- 「用量、成本與稽核紀錄」加入 `governance.llm_usage`（以 `call_id` 對照 `LiteLLM_SpendLogs`，超額欄位的意義與查詢範例：某位使用者本日的超額總和），`LiteLLM_VerificationToken` 一列補上 `budget_duration`；改寫「probe 沒有定價」為「probe 有測試單價，以 `model_group` 篩掉」
- 環境變數表加入 `GOV_BUDGET_TIMEZONE`、`GOV_USAGE_INFLIGHT_TTL_SECONDS`
- 新增對帳指令的說明與建議的執行頻率
- 「快速開始」與「開發」加入 `demo_m5` 與測試數量（實際執行 `uv run pytest -q`、`uv run pytest -m integration -q`、`opa test` 的結果）
- 「已知限制」：最後一批請求會超過上限（上界見 spec §8.6）；LLM 路徑依賴 Postgres；結算延遲超過 TTL 時實際超額可能大於上界；被注入的 agent 可大量觸發拒絕稽核；移除「LLM 請求因版本未註冊或 agent 已停用被拒絕時不寫稽核」
- 「開發進度」加入 M5；「文件」加入 M5 spec 與 findings

- [ ] **Step 3: AGENTS.md**

- 開發階段表加入 M5：「依委託使用者的 LLM 額度：LLM 路徑驗證使用者 token、拒絕 client 自帶的 end user 身份；依（agent, 使用者）的日月預算、rpm、tpm、同時進行數與單次上限，事後累計並逐筆記錄超額；key 預算改為每月重置」，狀態「完成」
- 目錄結構加入 `engine/limits.py`、`ledger.py`、`pricing.py`、`llm_gate.py`、`reconcile.py`
- 升級 LiteLLM 的說明：整合測試的數量改為實際數字，加上 `demo_m5`；依賴的 LiteLLM 行為加上 `secret_fields.raw_headers`、`async_log_success_event` 的欄位、auth 先於 guardrail、`LiteLLM_SpendLogs` 的欄位（對帳）
- 安全段落加入：「LLM 路徑的使用者身份只來自驗證過的 token；`user`、`metadata.user_id`、`x-litellm-end-user-id` 等 client 自帶的身份一律拒絕，不要改成覆寫」、「不要設定 LiteLLM 的 `user_header_mappings`、`user_header_name`、`max_end_user_budget_id`」、「受治理的模型別名與 fallback 都要有單價」

- [ ] **Step 4: 架構圖**

`docs/images/architecture.svg`：LLM 流量的能力加入「使用者身份」與「依使用者的額度」，顏色標為 M5，圖例加上 M5。以瀏覽器打開確認文字沒有重疊。

```bash
git add README.md AGENTS.md docs
git commit -m "docs: document M5 per-user LLM limits"
```

- [ ] **Step 5: workspace 的索引與學習文件**

- `../AGENTS.md` 專案索引：ai-gov-gateway 的主題加上「M5 依使用者的 LLM 額度」，狀態改為「M5 完成」
- `../docs/ai-gov-gateway/`：新增一份 M5 的學習文件（為什麼不用 LiteLLM 的 end user 預算、事後累計與預留的取捨、超額的上界與逐筆計算、面試問題「某位使用者把 agent 額度用光怎麼辦」的回答），數字引用 `var/m5-demo.txt` 與 findings；`build_html.py` 加入分頁並重建 HTML

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
| §4.2 token 的帶法 | 1（B1、B2）、3、9、11 |
| §4.3 處理順序、§4.4 fail-closed | 8、9 |
| §4.5、§12 kyt_agent | 計畫標頭的「上線順序」；kyt_agent 另開計畫 |
| §5 方案比較 | spike 文件（L1–L10） |
| §6 範圍與週期 | 4（等級）、5（日曆期別）、11（key 的 `budget_duration`） |
| §7 流量與單次上限 | 3（`text_length`）、4、5、8 |
| §8.1 帳本、§8.2 放行 | 6 |
| §8.3 結算時機 | 1（B3–B6）、9 |
| §8.4 超額計算 | 5、6、8、12 |
| §8.5 對帳 | 10、12 |
| §8.6 超額上界 | 4（等級數值）、13（B8） |
| §9 OPA | 4 |
| §10 錯誤碼與稽核 | 2、5、8 |
| §11 待驗證的假設 | 1、13 |
| §13 測試與驗收 | 4–13 |
| §14 風險 | 7（單價）、10（結算遺失）、14（已知限制） |
| §15 要更新的文件 | 14 |
| §16 第二階段 | 不在本計畫 |
