# M5 依委託使用者限制 LLM 用量設計

- 日期：2026-11-13
- 狀態：設計中，尚未實作；實作計畫見 [docs/plans/2026-11-13-user-budget.md](../plans/2026-11-13-user-budget.md)
- 前一階段：[M4 LLM guardrail 設計](2026-10-23-m4-guardrails-design.md)
- 驗證紀錄：[LiteLLM v1.105.1 的靜態檢查與待驗證項目](../spikes/m5-user-budget-spike.md)

## 1. 背景

目前 LLM 的預算與限流只綁在 virtual key 上。每個 agent 版本一把 key（`scripts/make_keys.py` 的 `KYT_LLM`：`rpm_limit: 60`、`tpm_limit: 300000`、`max_budget: 5.0`，沒有 `budget_duration`），所有委託使用者共用這份額度。問題有三個：

1. **共用額度**：任何一位分析師（或替他工作、被注入的 agent）都能把整把 key 的預算與 tpm 用光，其他人跟著被擋
2. **事後累計**：LiteLLM 在回應後才累計用量（M4 findings A8），跨過上限的那一筆仍會放行，並行的請求也都會放行
3. **LLM 路徑不知道使用者是誰**：只有 MCP 路徑（`/mcp`）驗證 Keycloak token；LLM 路徑（`/v1`）只從 key 解析 agent 與版本。請求裡的 `user` 欄位由 agent 自己填，不能當成身份

`max_budget` 是累計總額且沒有週期，用完之後 agent 會一直停擺，直到有人手動調整。

## 2. 目標與範圍

**目標**：同一個 agent 的額度依委託使用者切開。某位使用者用完自己的額度時，只有他的請求被擋，而且在呼叫模型之前就擋下；其他使用者不受影響，每次拒絕都留下稽核。

### 範圍內

- LLM 路徑驗證使用者 token：與 MCP 路徑同一套 `TokenVerifier`，fail-closed
- 拒絕 client 自帶的 end user 身份（body 的 `user`、`metadata.user_id`，以及 `x-litellm-customer-id`、`x-litellm-end-user-id` header）
- 依（agent, 使用者）的美元預算，以日、月為週期；依（agent, 使用者）的 rpm、tpm；單次請求的輸出上限
- 呼叫前預留、回應後結算的用量帳本（Postgres 的 `governance` 資料庫）
- 額度由 OPA 依 agent 與使用者角色提供，資料以 GitOps 維護
- 拒絕寫入既有的 hash chain 稽核
- key 層的預算改成有週期（`budget_duration`），作為 agent 總額度的最後一道防線

### 不在範圍內

- 跨 agent 的使用者總額度：目前只有 kyt 一個 agent，帳本保留 `agent_id` 欄位，之後要加時只是多一條查詢（§6.1）
- 臨時調高額度的審批流程（例如由 Supervisor 核准一次加額）：留待後續，可沿用 maker-checker
- 查詢自己剩餘額度的 API 或 `agw` 指令
- 與 `LiteLLM_SpendLogs` 的自動對帳（§8.5 的已知限制）
- MCP 路徑的 tool 呼叫次數限制：tool 呼叫已經由 policy 與審批控管
- kyt_agent 的程式修改：另開 kyt_agent 的計畫，本文件只列出需要的改動（§12）

## 3. 結論

**建議方案 B：由治理引擎在 Postgres 依（agent, 使用者）記帳，pre-call 預留、回應後結算。** LiteLLM key 的預算保留為 agent 總額度。

| 項目 | 決策 | 理由 |
| ---- | ---- | ---- |
| 使用者身份 | 只取驗證過的 token 的 `sub`；client 自帶的 end user 身份一律拒絕 | `user` 等欄位由 agent 填寫，被注入的 agent 可以偽造或輪換（spike L2、L3） |
| token 的帶法 | key 放 `x-litellm-api-key`，使用者 token 放 `Authorization: Bearer` | 與 MCP 路徑相同；LiteLLM 認證時 `x-litellm-api-key` 優先（L9），hook 可從 `secret_fields` 取得原始 header（L8，待 B1 確認） |
| 預算機制 | 方案 B，不用 LiteLLM 的 end user 預算 | LiteLLM 在 guardrail 之前就用 client 自填的 id 檢查預算（L4），A 的原始做法無效；改成在 middleware 注入仍是事後累計、沒有預留、拒絕不進稽核（§5） |
| 跨過上限 | 預留「最壞情況」的成本，加總不超過上限才放行 | 有預留才能讓並行請求與「最後一筆」都在呼叫模型之前判斷；強制輸出上限讓最壞情況有界 |
| 範圍 | 兩層：（agent, 使用者）由 gateway 強制；agent 總額由 LiteLLM key 預算兜底 | 面試的問題是「一位使用者把 agent 的額度用光」，切開的正是這一層；key 預算防止使用者數乘上個人額度超出 agent 的總預算 |
| 週期 | 個人額度以日、月計，依 `Asia/Taipei` 的日曆重置；key 加上 `budget_duration: "1mo"` | 日額度符合分析師的工作節奏，說明時也直觀（「今天的額度用完了」） |
| rpm / tpm | 一併做，依（agent, 使用者） | rpm 在 pre-call 精確計數；tpm 與預算共用帳本與預留，成本低 |
| 輸出上限 | gateway 強制 `max_tokens`：沒帶就補上限，超過上限就拒絕 | 沒有輸出上限時 Gemini 單次最多 65,536 個輸出 token，約 $0.25，預留會大到不可用（L10） |
| 額度來源 | OPA 依 agent 與使用者角色提供（`governance.llm` 擴充） | 與 guardrail 設定同一個來源、同樣以 GitOps 審查；policy 只回傳數值，計數由引擎做 |
| 拒絕的狀態碼 | 預算用完 403（不可重試）、rpm/tpm 429（可重試）、token 問題 401 | kyt 會自動重試 429；預算要到隔天才恢復，回 429 只會浪費重試 |
| 稽核 | 新事件 `llm_access`，只記拒絕；放行的請求記在帳本，不進 hash chain | 每次 LLM 呼叫都寫 hash chain 會被全域鎖序列化；帳本是計量紀錄，拒絕才是治理決策 |

**尚未驗證、需要 spike 確認的假設**（詳見 §11 與 spike 文件的 B1–B10）：hook 拿得到原始 `Authorization`（B1、B2）、結算事件的時機與 `call_id`（B3–B6）、Gemini 的 `max_tokens` 是否涵蓋 reasoning token（B7）、以字元數估計輸入 token 是否偏高（B8）、key 的 `"1mo"` 週期（B9）、改寫 `max_tokens` 會送到 provider（B10）。

### 元件

| 模組 | 職責 |
| ---- | ---- |
| `engine/llm_gate.py`（新） | `LlmGate`：§4.3 的處理順序、拒絕的稽核；結算與釋放 |
| `engine/limits.py`（新） | 額度等級的資料模型；日曆期別與額度判斷（純函式） |
| `engine/ledger.py`（新）、`engine/postgres.py` | 帳本介面；`llm_usage` 表與預留、結算、釋放的交易 |
| `engine/pricing.py`（新） | 每個模型別名（含 fallback）的最高單價與最壞成本估計 |
| `engine/guardrails/service.py` | 拆成 `resolve()`（agent 與 OPA 設定）與 `guard_request()`（M4 的處理） |
| `engine/policy.py` | `llm_config(agent, user)` |
| `hook_adapter.py` | 從 raw headers 取 token、偵測自帶的 end user header、從 logging 事件取出結算資料 |
| `plugin.py` | pre-call 改呼叫 `LlmGate`；success logging 事件結算；failure hook 釋放 |

`engine/` 仍不 import LiteLLM：價格表由 plugin 從 LiteLLM 的 router 與 `model_cost` 讀出 plain data 後交給引擎。

## 4. LLM 路徑的使用者身份

### 4.1 為什麼不能用 `user` 欄位

`user`、`metadata.user_id`、`safety_identifier` 與 `x-litellm-end-user-id` 等 header 都由 client 提供（L2）。agent 讀到被注入的 tool 結果後，可以在下一次請求換一個值；LiteLLM 預設也不檢查這個 id 是否存在（L3）。身份只能來自 gateway 驗證過的 token。

處理方式是**拒絕**而不是覆寫：

- LiteLLM 在 hook 之前就用這些值建立 end user 並記在 `LiteLLM_SpendLogs.end_user`（L4），覆寫也改不了紀錄，只會留下一個看起來可信的假身份
- 拒絕讓「`end_user` 欄位永遠是空的」成為不變式；依使用者的用量以 `litellm_call_id` 對照帳本（§8.1）
- kyt 目前不送這些欄位，拒絕不影響它

body 的 `user`、`metadata.user_id`、`metadata.user` 與兩個標準 header（hook 從 raw headers 檢查）都由 pre-call 的第 2 步拒絕並寫稽核。hook 之前 LiteLLM 已讀過這些值，但請求在送出前就被拒絕。`fields.py` 的白名單保留 `user`，交給這一步處理，拒絕才會留下稽核；`user_header_mappings`、`user_header_name` 不設定，`litellm_metadata` 與 `safety_identifier` 已不在白名單。

### 4.2 token 怎麼帶

```
POST /v1/chat/completions
x-litellm-api-key: Bearer sk-…        ← agent 版本的 virtual key
Authorization: Bearer eyJ…           ← 委託使用者的 Keycloak access token
```

- 與 MCP 路徑相同（kyt v1.5 的 `GatewayExecutor` 就是這樣送），一個 client 設定兩條路徑都能用
- LiteLLM 以 `x-litellm-api-key` 認證（L9）；`Authorization` 不會被當成 key
- hook 從 `data["secret_fields"]["raw_headers"]["authorization"]` 取 token（L8）。`proxy_server_request.headers` 已遮蔽，不能用
- `AuthorizationHeaderGuard` 已保證只有一個 `Authorization`，不會出現稽核與實際身份不一致
- 沒有帶 `x-litellm-api-key` 時，LiteLLM 會拿 JWT 當 key 認證而失敗（401），一樣是拒絕

B1 或 B2 不成立時的替代：自訂 header `x-gov-user-token`，由 ASGI middleware 取出後放進 request state。缺點是兩條路徑的帶法不同。

### 4.3 在 pre-call 的哪一步驗證

M4 的處理順序加入三步（粗體為新增）：

1. 只接受 `acompletion`
2. 檢查請求欄位；**拒絕 client 自帶的 end user 身份**（§4.1）
3. **驗證使用者 token**：與 MCP 路徑相同的 `TokenVerifier`（簽章、issuer、audience、到期、`azp` 在允許清單、`sub` 必填），以 `asyncio.to_thread` 執行
4. 從 key 解析 agent 與版本，向 OPA 查詢註冊狀態、guardrail 設定與**此使用者的額度**（一次查詢）
5. **檢查輸出上限**：沒帶 `max_tokens` / `max_completion_tokens` 就補上限，超過就拒絕
6. 檢查大小
7. 注入檢查、PII 假名化、spotlighting、guardrail 稽核（M4 不變）
8. **預留額度**：估計最壞成本，檢查日、月預算與 rpm、tpm，通過才寫入帳本

token 放在 OPA 之前：無效 token 不必查 OPA，也避免把未驗證的 claims 送進 policy。輸出上限放在 guardrail 之前：被拒絕的請求不必做 PII 偵測，也不會留下 `llm_guardrail` 稽核。預留放在最後：前面任何一步拒絕都不會留下預留，不需要釋放。預留之後還可能被 LiteLLM 自己的 key 限流擋下（guardrail 在 rate limiter 之前執行，M4 findings），此時由 failure hook 釋放（§8.3）。

### 4.4 fail-closed

| 情況 | 結果 |
| ---- | ---- |
| 沒有 `Authorization` | 401 `missing_user_token` |
| 簽章、issuer、audience、到期錯誤，`azp` 不在允許清單（服務帳號），取不到 JWKS | 401 `invalid_user_token` |
| client 自帶 end user 身份 | 400 `end_user_override` |
| OPA 沒回應 / 回傳不完整 | 503 `policy_unavailable` / `policy_error`（沿用） |
| 使用者沒有任何符合的額度等級 | 403 `no_llm_quota` |
| 帳本資料庫連不上、預留失敗、模型沒有價格 | 503 `budget_unavailable` |
| 拒絕時稽核寫入失敗 | 503 `audit_write_failed`（沿用） |

### 4.5 對 kyt_agent 的影響

kyt 的 LLM client 目前只送 virtual key（`Authorization: Bearer <key>`，見 kyt_agent `graph/deps.py` 的 `_proxy_kwargs`）。M5 上線後未帶 token 的請求一律 401，所以 kyt 要先改版（§12），以新版本 `kyt-agent@1.6` 註冊後再切換。Keycloak 的 access token 只有 300 秒（`realm-governance.json` 的 `accessTokenLifespan`），一個案件跨數分鐘，kyt 必須在每次請求前取得未過期的 token。

## 5. 方案比較

### 方案 A：交給 LiteLLM 的 end user 預算

原始構想是 pre-call hook 用 token 的 `sub` 覆寫 `user`。依 v1.105.1 的原始碼（spike L1–L7）：

| 問題 | 結果 |
| ---- | ---- |
| end user 預算存在嗎 | 存在（L1），也支援 rpm、tpm（L5）與週期重置（L7） |
| 在呼叫前檢查嗎 | 在 auth 階段檢查，早於 guardrail；但 end user id 也是在那時候、從 client 的欄位決定（L2、L4） |
| hook 覆寫 `user` 有效嗎 | **無效**：預算檢查與花費歸屬都用 auth 階段的 id，不讀 `data["user"]`（L4） |
| 與 key 預算如何疊加 | 並行檢查，任一超過就拒絕（L1）；兩者都是事後累計 |
| 跨過上限 | 與 key 相同：花費在回應後累計，判斷式是 `>`（L1、L6），並行請求全部放行 |
| fallback 會重算嗎 | 預算檢查在 auth，rate limit fallback 重跑的是 pre-call，不會重跑 auth；花費只記實際成功的那次（待 B3 確認） |

要讓 A 可行，得改成 **A'**：在 LiteLLM 認證之前加 ASGI middleware，驗證 token、移除 client 帶的所有 end user 來源（六種，L2），再補上 `x-litellm-end-user-id: <sub>`。A' 仍有這些問題：

- **事後累計**：最後一筆與並行請求都會超過上限，這正是面試被問到的情況
- **拒絕不在我們的稽核裡**：LiteLLM 丟 `BudgetExceededError`，格式不是 `{"governance": "denied"}`，也不會寫 hash chain
- **依角色的額度要同步兩份資料**：OPA 的角色設定要轉成 LiteLLM 的 budget table 與每位 end user 的 `budget_id`
- **middleware 不知道 agent**：它在 key 認證之前執行，額度只能依使用者，不能依（agent, 使用者），除非把 id 組成 `<agent>:<sub>`，但 middleware 那時還不知道 key 屬於哪個 agent
- **依賴 LiteLLM 內部優先序**：六種來源的優先序是實作細節（L2），升級時改變就會被繞過
- 沒有 Redis 時，`spend:end_user:*` 計數在各 worker 的記憶體，多 worker 時不準

### 方案 B：治理引擎記帳（建議）

pre-call 依（agent, 使用者）在 Postgres 預留最壞情況的成本，成功回應後以 LiteLLM 算出的實際成本結算，失敗時釋放。細節見 §8。

| 面向 | 方案 A' | 方案 B |
| ---- | ------- | ------ |
| 身份來源 | middleware 驗證 token 後注入 header | hook 驗證 token（與 MCP 同一套） |
| 判斷時機 | auth，事後累計 | pre-call，預留後判斷 |
| 最後一筆、並行請求 | 會超過上限 | 估計不低於實際時不會超過（§8.2） |
| 範圍 | 依使用者 | 依（agent, 使用者），可擴充到跨 agent |
| 額度來源 | LiteLLM budget table（需同步） | OPA，依角色 |
| 拒絕的格式與稽核 | LiteLLM 的錯誤，無稽核 | governance JSON，寫 hash chain |
| 新增的程式 | middleware、同步腳本 | 帳本、預留與結算、價格估計 |
| 依賴的 LiteLLM 行為 | end user 的六種來源與優先序 | `secret_fields`、結算事件（B1–B6） |

B 的程式比較多，但每一項都在我們能測試的範圍內，也和既有的治理一致：身份來自 token、額度來自 OPA、拒絕寫 hash chain、無法判斷就拒絕。

## 6. 預算的範圍與週期

### 6.1 範圍

| 層 | 誰強制 | 用途 |
| -- | ------ | ---- |
| （agent, 使用者） | gateway 帳本 | 一位使用者不能用光 agent 的額度；面試問題的答案 |
| agent 版本（key） | LiteLLM key 的 `max_budget` + `budget_duration` | agent 的總額度，防止「使用者數 × 個人額度」超過預算；事後累計，作為最後一道防線 |
| 使用者跨 agent | 不做 | 目前只有一個 agent。帳本以 `(agent_id, user_sub)` 記錄，要加時是 `WHERE user_sub = ?` 的另一個加總，額度放在 OPA 的另一個欄位 |

不只做「依使用者」：不同 agent 的成本結構與負責人不同，kyt 一個案件約 $0.08，其他 agent 可能高很多，同一份額度很難同時適用。

### 6.2 個人額度的週期

- `day` 與 `month` 兩種，可以只設其中一種；兩者都設時都要符合
- 依 `GOV_BUDGET_TIMEZONE`（預設 `Asia/Taipei`）的日曆重置，例如每天 00:00
- 以預留的時間（`created_at`）決定屬於哪一期：跨過午夜的請求算在開始那天
- 拒絕的回應帶 `resets_at`，讓 agent 與使用者知道何時恢復

### 6.3 key 的總額度

目前 `max_budget: 5.0` 沒有週期，用完就永久停擺。改為 `budget_duration: "1mo"`，`max_budget` 依「預期使用者數 × 個人月額度」設定，示範環境用 30.0。`"1mo"` 由 LiteLLM 的 reset job 處理，從 key 建立或上次重置起算，不是日曆月（B9 確認）。

## 7. 依使用者的 rpm、tpm 與輸出上限

### 7.1 rpm

在預留的同一個交易中，計算此（agent, 使用者）過去 60 秒內被放行的請求數，加上這一筆超過 `rpm` 就拒絕。只計放行的請求，被拒絕的不計，否則持續送請求會一直鎖住自己。這是精確的事前檢查，不會超過。

### 7.2 tpm

同樣在預留的交易中：過去 60 秒內已結算請求的實際 token，加上未結算預留的估計 token，再加上這一筆的估計，超過 `tpm` 就拒絕。估計的 token 是「輸入估計 + 輸出上限」（§8.2），所以 tpm 判斷偏保守。

key 的 `tpm_limit` 與 `rpm_limit` 保留，作為 agent 的總量。個人 tpm 應明顯低於 key 的 tpm（示範值 150,000 對 300,000），一位使用者才不會佔滿整把 key。

### 7.3 單次輸出上限

目前 `max_tokens` 由 agent 自帶，gateway 不強制；輸入已有 `GOV_GUARDRAIL_MAX_CHARS`、`GOV_GUARDRAIL_MAX_TOTAL_CHARS`。M5 加入 `max_output_tokens`（OPA，依等級）：

- 請求沒帶 `max_tokens` 也沒帶 `max_completion_tokens`：補上 `max_tokens = max_output_tokens`
- 任一個超過上限：400 `max_tokens_exceeded`，不截斷。截斷會讓 agent 收到不完整的回應而不自知
- 預留以實際生效的上限計算

這是預留能成立的前提：沒有輸出上限時，Gemini 單次最多 65,536 個輸出 token（約 $0.25），每個請求都得預留這麼多。Gemini 的 reasoning token 是否算在 `max_tokens` 內待 B7 確認；不算時預留要另加 reasoning 的上限。

## 8. 預留與結算

### 8.1 帳本

`governance` 資料庫新增一張表（不進 hash chain）：

```sql
CREATE TABLE IF NOT EXISTS llm_usage (
    owner text NOT NULL,              -- key 的雜湊，與 PendingPseudonyms 相同（owner_of）
    call_id text NOT NULL,            -- litellm_call_id，可對照 LiteLLM_SpendLogs
    agent_id text NOT NULL,
    user_sub text NOT NULL,
    model text NOT NULL,
    status text NOT NULL,             -- reserved | settled | released
    reserved_usd numeric NOT NULL,
    reserved_tokens integer NOT NULL,
    cost_usd numeric,                 -- 結算時的實際成本
    tokens integer,                   -- 結算時的實際 token（輸入 + 輸出）
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,  -- 未結算的預留在此之後不再佔用額度
    settled_at timestamptz,
    PRIMARY KEY (owner, call_id)
);
CREATE INDEX IF NOT EXISTS llm_usage_user ON llm_usage (agent_id, user_sub, created_at);
```

一筆請求在某一期佔用的金額：

| 狀態 | 佔用 |
| ---- | ---- |
| `settled` | `cost_usd` |
| `reserved` 且未過期 | `reserved_usd` |
| `reserved` 已過期、`released` | 0 |

主鍵含 `owner`：`call_id` 可由 client 以 `x-litellm-call-id` 指定，不同 key 可能重複（M4 的對照表也是這樣處理）。同一把 key 重複使用 `call_id` 時插入失敗，回 503 `budget_unavailable`。

不記 PII：`user_sub` 是 Keycloak 的 UUID，與 `approvals`、`audit_events` 相同；不記訊息內容。

### 8.2 估計最壞成本

```
estimate_tokens = input_estimate + max_output_tokens
estimate_usd    = input_estimate × 最高輸入單價 + max_output_tokens × 最高輸出單價
```

- **輸入估計**：請求中 `messages` 與 `tools` 的 JSON 字元數（guardrail 改寫後）。一般 tokenizer 每個 token 至少涵蓋一個字元，所以字元數不低於 token 數，JSON 的引號與鍵名讓它更保守。罕見字元被拆成多個 byte token 時可能低估，待 B8 以 kyt 的真實請求確認
- **單價**：取請求模型與它所有 fallback 中最高的單價（`kyt-main` 為 Gemini，fallback 為較便宜的 `gpt-6-luna`，取 Gemini）。單價來自 LiteLLM 的 `model_cost`，deployment 有 `model_info` 時以它為準，與 LiteLLM 計費同一個來源
- 找不到單價：503 `budget_unavailable`，不放行。probe 模型在 `litellm/config.yaml` 以 `model_info` 加上測試單價
- 估計放在 gateway 的 plugin 層（需要 LiteLLM 的 router 與價格表），引擎只接收數字，維持 `engine/` 不 import LiteLLM

**跨過上限的保證**：只要估計不低於實際，任何時候「已結算 + 未結算預留」都不超過上限，因此最後一筆與並行請求都不會超過。估計低於實際時（B7、B8 不成立），超出的部分以「進行中請求的低估量」為上限，並在下一次預留前反映到已結算金額。

以 kyt 為例：輸入 30,000 字元、輸出上限 8,192 時，預留約 $0.053（30,000 × 7.5e-7 + 8,192 × 3.75e-6）；實際平均一次約 $0.008。預留偏高只會降低同時進行的請求數（日額度 $2 可同時預留約 37 筆），不影響最終計費。

### 8.3 結算的時機

| 事件 | 動作 |
| ---- | ---- |
| `async_log_success_event`（LiteLLM 計費完成，含 streaming 結束） | `status = settled`，寫入 `cost_usd`、`tokens`；不論原本是否已釋放或過期都更新，實際有花費就要記 |
| `async_post_call_failure_hook`（請求最終失敗，含被 LiteLLM 限流擋下、自己的 hook 拒絕） | 只把 `reserved` 改為 `released`；已 `settled` 的不動 |
| 都沒有收到 | `expires_at` 之後不再佔用額度 |

- 不用 `async_log_failure_event` 釋放：router 重試與 fallback 前的每次失敗可能都會觸發（B4），那時請求還在進行
- `async_post_call_success_hook` 不用於結算：streaming 時不會呼叫（M4 findings A4），也拿不到成本
- 結算寫入失敗只能記錯誤日誌，回應已經送出。該筆預留到期後不再佔用額度，造成低估（§8.5）

### 8.4 fallback、重試與 streaming

- **router fallback**（主力失敗改送 fallback）：同一個 `call_id`（A6），預留一次；預留已用最高單價；成功的那次結算實際成本
- **rate limit 觸發的 fallback**：proxy 以新的 `call_id` 重跑 pre-call，第二筆預留收不到任何事件（M4 findings，待 B5 確認），只能等 `expires_at`。影響是 429 之後該使用者的可用額度暫時少一筆預留，最多 `GOV_BUDGET_RESERVATION_TTL_SECONDS`（預設 600，與對照表的回收時間相同）
- **重試**：kyt 自己重試時是新的請求，各自預留與結算
- **streaming**：預留在 pre-call，結算在串流結束後的 success 事件（B3）。client 中途斷線時是否有事件待 B6 確認，沒有時靠 `expires_at`

### 8.5 並行與一致性

預留在一個交易中完成：

```sql
SELECT pg_advisory_xact_lock(72410002, hashtext(agent_id || ':' || user_sub));
-- 加總本日、本月的佔用，過去 60 秒的請求數與 token
-- 全部在額度內才 INSERT 一筆 reserved
```

- 以（agent, 使用者）為單位序列化，不同使用者互不等待。使用雙參數的 advisory lock，與稽核的 `AUDIT_LOCK_KEY` 不同命名空間
- 拒絕的稽核在這個交易之外寫入，不同時持有兩把鎖
- 多個 gateway worker 共用同一個 Postgres，計數一致；不依賴 LiteLLM 的記憶體計數

**已知限制**：

- 結算事件遺失（gateway 在回應後、結算前重啟，或結算寫入失敗）時，該筆的實際成本不會進帳本，造成低估。LiteLLM 的 `LiteLLM_SpendLogs` 仍有紀錄，之後可以用 `call_id` 對帳
- 帳本只增不刪，之後需要定期清除超過一個月的資料

## 9. OPA policy

額度放在 `policies/agents/data.json` 各 agent 的 `llm_limits`，依序比對等級，第一個符合使用者角色的生效（`"*"` 表示任何驗證過的使用者）：

```json
"llm_limits": [
  {
    "tier": "compliance_supervisor",
    "role": "compliance_supervisor",
    "budget_usd": {"day": 5.0, "month": 50.0},
    "rpm": 30,
    "tpm": 150000,
    "max_output_tokens": 8192
  },
  {
    "tier": "default",
    "role": "*",
    "budget_usd": {"day": 2.0, "month": 20.0},
    "rpm": 20,
    "tpm": 150000,
    "max_output_tokens": 8192
  }
]
```

- 用有序清單而不是「預設加角色覆寫」：每個等級的值都是完整的，不需要合併規則，審查時一眼就看得出某位使用者會套用哪些數字
- 已註冊但沒有 `llm_limits` 的 agent 套用最嚴格的預設（與 guardrail 的 `strict_guardrails` 相同的做法），例如 `day: 0.5`、`rpm: 10`、`tpm: 50000`、`max_output_tokens: 2048`
- 有 `llm_limits` 但沒有符合的等級（例如沒有 `"*"`）：`limits` 為 `null`，gateway 回 403 `no_llm_quota`
- 欄位缺漏或型別錯誤：`llm` 為 undefined，gateway 視為 `policy_error` 而拒絕
- OPA 的 input 從只有 `agent` 改為 `agent` 加 `user`（`sub`、`username`、`roles`），與 tool 呼叫的 input 相同
- `governance.llm` 的結果新增 `limits`（含 `tier`）。policy 只回傳數值，計數與比較由引擎做：OPA 沒有狀態，不適合累計
- 修改後更新 `policies/meta/data.json` 的 `policy_version` 並通過 `opa test`

## 10. 錯誤碼與稽核

### 10.1 錯誤碼

格式沿用 M4：HTTP 狀態碼不變，`error.provider_specific_fields` 為 governance JSON。新增 `limit` 欄位說明是哪一種額度與何時恢復，不含金額。

| 狀態碼 | 代碼 | 發生情況 | 額外欄位 |
| ------ | ---- | -------- | -------- |
| 401 | `missing_user_token` | 沒有 `Authorization` | |
| 401 | `invalid_user_token` | token 無法驗證，或不是允許的 client 發的 | |
| 400 | `end_user_override` | body 的 `user`、`metadata.user_id`、`metadata.user`，或 `x-litellm-customer-id`、`x-litellm-end-user-id` header | `field` |
| 400 | `max_tokens_exceeded` | `max_tokens` 或 `max_completion_tokens` 超過等級的上限 | `field` |
| 403 | `no_llm_quota` | 沒有符合使用者角色的額度等級 | |
| 403 | `user_budget_exceeded` | 本日或本月的預算不足以預留這一筆 | `limit: {"type": "budget_day", "resets_at": "…"}` |
| 429 | `user_rate_limited` | 過去 60 秒的請求數或 token 超過 | `limit: {"type": "rpm", "retry_after_seconds": 12}` |
| 503 | `budget_unavailable` | 帳本無法讀寫、`call_id` 重複、模型沒有單價 | |

```json
{"governance": "denied", "reasons": ["user_budget_exceeded"], "limit": {"type": "budget_day", "resets_at": "2026-11-14T00:00:00+08:00"}}
```

### 10.2 稽核事件 `llm_access`

每一次 M5 新增的拒絕（上表除了 503 `budget_unavailable` 之外）都寫一筆，接在同一條 hash chain：

| 欄位 | 內容 |
| ---- | ---- |
| `event_type` | `llm_access` |
| `agent_id`、`agent_version`、`key_alias` | 呼叫的 agent 與 key（key 名稱格式不符時為 `null`） |
| `user_sub`、`username` | 驗證過的使用者；token 無效時為 `null` |
| `decision` | `deny` |
| `reasons` | 錯誤代碼 |
| `policy_version` | 有查到 OPA 時填入 |
| `details.call_id`、`details.model` | 可對照 `LiteLLM_SpendLogs` |
| `details.tier` | 套用的額度等級 |
| `details.limit` | 額度的種類與上限，例如 `{"type": "budget_day", "limit_usd": 2.0}` |
| `details.usage` | 判斷時的已結算、未結算預留與這一筆的估計，例如 `{"settled_usd": 1.97, "reserved_usd": 0.02, "estimate_usd": 0.053}`；rpm、tpm 時為次數或 token |

- 不記 token 原文、訊息內容與 PII 原值；金額與次數不是個資
- 放行的請求不寫稽核，計量紀錄在帳本，以 `call_id` 對照
- token 無效、未帶 token 也寫稽核：這是有人以 agent 的 key 嘗試不帶身份使用 LLM，值得留下紀錄。順帶把 M4 的已知限制「LLM 請求因版本未註冊或 agent 已停用被拒絕時不寫稽核」一起補上，改為寫 `llm_access`

## 11. 尚未驗證的假設

靜態檢查（spike L1–L10）已確認方案 A 的限制與 B 需要的入口存在；以下需要執行期驗證，結果決定 B 的細節，不影響 A 與 B 的選擇。

| 編號 | 假設 | 影響 | 不成立時 |
| ---- | ---- | ---- | -------- |
| B1、B2 | hook 從 `secret_fields.raw_headers` 取得 `Authorization`，LiteLLM 以 `x-litellm-api-key` 認證、不轉送 JWT | §4.2 token 的帶法 | 改用 `x-gov-user-token` + middleware |
| B3 | guardrail 會收到 `async_log_success_event`，帶相同 `call_id` 與實際成本，streaming 與 fallback 各一次 | §8.3 結算 | 另註冊一個結算用的 `CustomLogger` |
| B4 | router 重試的每次失敗都會觸發 `async_log_failure_event` | §8.3 不用它釋放 | 無影響 |
| B5 | rate limit fallback 的第二個 `call_id` 收不到事件 | §8.4 | 改由事件釋放 |
| B6 | streaming 中斷時的事件與成本 | §8.4 | 列為已知限制 |
| B7 | Gemini 的 `max_tokens` 涵蓋 reasoning token | §7.3、§8.2 估計上限 | 預留另加 reasoning 上限 |
| B8 | JSON 字元數不低於 `prompt_tokens` | §8.2 估計 | 乘上實測係數 |
| B9 | key 接受 `budget_duration: "1mo"` | §6.3 | 改用 `"30d"` |
| B10 | 改寫的 `max_tokens` 會送到 provider，fallback 沿用 | §7.3 | 只拒絕、不補值 |

## 12. kyt_agent 需要的改動（另開計畫）

- LLM client 改為送 `x-litellm-api-key: Bearer <key>` 與 `Authorization: Bearer <使用者 token>`。以 `httpx.Auth` 在每次請求前取得 token，到期前 30 秒更新，傳給 `init_chat_model` 的 `http_client` / `http_async_client`
- 取得 token 的程式與 `GatewayExecutor._token()` 共用（同一個使用者、同一個 `kyt-cli` client）
- 錯誤處理：403 `user_budget_exceeded` 停止此案件並在報告與 audit log 寫明「額度已用完」與 `resets_at`；429 `user_rate_limited` 依 `retry_after_seconds` 等待後重試；400 `max_tokens_exceeded` 不應發生，視為設定錯誤
- 不送 `user` 欄位（目前已經沒有送）
- 以新版本 `kyt-agent@1.6` 註冊；eval 改用專用的測試使用者與等級，避免批次 eval 用光分析師的日額度
- 未啟用治理（直連 Gemini）時行為不變

## 13. 測試與驗收

- **單元測試**：token 取出與 end user 欄位檢查（`hook_adapter`）、額度等級的模型驗證、估計（`pricing`）、預留的判斷（fake 帳本，含期別邊界、時區、過期預留、rpm/tpm 視窗）、`LlmGuardrail` 的新順序與稽核
- **Rego 測試**：等級的順序與 `"*"`、沒有 `llm_limits` 的嚴格預設、沒有符合等級時為 `null`、欄位不完整時 `llm` 為 undefined
- **整合測試**（`governance_test` 資料庫）：帳本的預留、結算、釋放與過期；20 個並行預留只放行額度內的數量，總和不超過上限；跨午夜的期別
- **契約測試**（gateway + probe）：B1–B6、B10 的行為固定成測試；拒絕的狀態碼與 `provider_specific_fields`；帳本的 `cost_usd` 與 `LiteLLM_SpendLogs.spend` 相同
- **端到端示範** `scripts/demo_m5.py`：缺 token、無效 token、服務帳號 token、自帶 `user`、自帶 header、`max_tokens` 過大、補上 `max_tokens`、低額度使用者用完預算、其他使用者不受影響、並行請求不超過上限、rpm、streaming 結算、fallback 結算
- **真實 LLM**：B7、B8 以真實 Gemini 驗證，成本 < $0.05
- **完成標準**：上述全部通過；`opa test` 通過；`uv run pytest -m integration` 全數 passed（不是 skipped）；`demo_m4` 改為帶 token 後仍全數通過

## 14. 風險

| 風險 | 緩解 |
| ---- | ---- |
| LLM 路徑多了 token 驗證與一次 Postgres 交易，延遲增加 | JWKS 有快取；預留只鎖單一使用者；在契約測試記錄延遲，與 M4 比較 |
| Postgres 停擺時所有 LLM 流量失敗 | fail-closed 的必然結果，與 OPA 相同，列入已知限制 |
| 被注入的 agent 大量送出超額請求，每次都寫 hash chain | 拒絕不經過模型，成本只有稽核寫入；必要時再加「同一使用者、同一原因 60 秒內只寫一筆並記數量」 |
| 估計偏低造成少量超過 | B7、B8 實測；結算後立即反映；key 預算兜底 |
| LiteLLM 升級改變 `secret_fields`、結算事件或 hook 順序 | 契約測試涵蓋，升級前必跑（AGENTS.md 既有規定） |
| 日額度讓長時間的調查中途停止 | 拒絕回 `resets_at`；kyt 在報告寫明原因；等級由 GitOps 調整 |

## 15. 要更新的文件

- `README.md`：設計原則表加入「依使用者的額度」；功能清單；LLM 路徑的處理順序（§4.3）；錯誤代碼表與 `llm_access` 稽核事件；「用量、成本與稽核紀錄」加入 `llm_usage`；`GOV_BUDGET_*` 環境變數；「fallback 與 tpm」改寫為兩層額度；已知限制（§8.5、§14）；`probe_llm` 有測試單價；開發進度加入 M5
- `AGENTS.md`：開發階段表加入 M5；目錄結構加入帳本與價格估計；升級 LiteLLM 前要跑的測試數量；安全段落加入「LLM 路徑的身份只來自 token，client 自帶的 end user 身份一律拒絕」
- `docs/images/architecture.svg`：LLM 流量加入「使用者身份」與「依使用者的額度」兩個能力，標為 M5
- 本 spec 實作後加上「實作後修訂」一節；spike 結果寫入 `docs/spikes/m5-findings.md`
- workspace 的 `AGENTS.md` 專案索引與 `docs/ai-gov-gateway/` 學習文件
