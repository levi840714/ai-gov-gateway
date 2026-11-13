# M5 依委託使用者限制 LLM 用量設計

- 日期：2026-11-13
- 狀態：第一階段已實作；實作時的調整見 §17。實作計畫見 [docs/plans/2026-11-13-user-budget.md](../plans/2026-11-13-user-budget.md)
- 前一階段：[M4 LLM guardrail 設計](2026-10-23-m4-guardrails-design.md)
- 驗證紀錄：[LiteLLM v1.105.1 的靜態檢查與待驗證項目](../spikes/m5-user-budget-spike.md)、[執行期驗證結果](../spikes/m5-findings.md)
- 修訂：初稿採「呼叫前預留」；討論後改為第一階段先做「事後累計 + 精確記錄超額」，預留的設計移到 §16，作為第二階段的紀錄

## 1. 背景

目前 LLM 的預算與限流只綁在 virtual key 上。每個 agent 版本一把 key（`scripts/make_keys.py` 的 `KYT_LLM`：`rpm_limit: 60`、`tpm_limit: 300000`、`max_budget: 5.0`，沒有 `budget_duration`），所有委託使用者共用這份額度。問題有三個：

1. **共用額度**：任何一位分析師（或替他工作、被注入的 agent）都能把整把 key 的預算與 tpm 用光，其他人跟著被擋
2. **超額沒有上界、也沒有紀錄**：LiteLLM 在回應後才累計用量（M4 findings A8），跨過上限的那一筆與並行的請求都會放行，事後也查不出超出多少、是哪幾筆造成的
3. **LLM 路徑不知道使用者是誰**：只有 MCP 路徑（`/mcp`）驗證 Keycloak token；LLM 路徑（`/v1`）只從 key 解析 agent 與版本。請求裡的 `user` 欄位由 agent 自己填，不能當成身份

`max_budget` 是累計總額且沒有週期，用完之後 agent 會一直停擺，直到有人手動調整。

## 2. 目標與範圍

**目標**：同一個 agent 的額度依委託使用者切開。某位使用者用完自己的額度後，只有他之後的請求被擋，其他使用者不受影響；超出額度的金額與 token 精確記錄到每一筆請求，並寫入稽核。

第一階段採**事後累計**：呼叫前只看已結算的用量，回應後以實際成本結算。跨過上限的最後一批請求仍會放行，但超出量有明確上界（§8.6），而且每一筆超出多少都記錄下來。

### 範圍內

- LLM 路徑驗證使用者 token：與 MCP 路徑同一套 `TokenVerifier`，fail-closed
- 拒絕 client 自帶的 end user 身份（body 的 `user`、`metadata.user_id`、`metadata.user`，以及 `x-litellm-customer-id`、`x-litellm-end-user-id` header）
- 依（agent, 使用者）的美元預算，以日、月為週期；依（agent, 使用者）的 rpm、tpm、同時進行的請求數；單次請求的輸入與輸出上限
- 用量帳本（Postgres 的 `governance` 資料庫）：放行時記一筆，回應後以 LiteLLM 計算的實際成本結算，**結算時計算這一筆超出各項額度的量**
- 超額寫入 hash chain 稽核（`llm_limit_exceeded`）；拒絕寫入 `llm_access`
- 對帳指令：結算事件遺失時，以 `LiteLLM_SpendLogs` 補結算
- 額度由 OPA 依 agent 與使用者角色提供，資料以 GitOps 維護
- key 層的預算改成有週期（`budget_duration`），作為 agent 總額度的最後一道防線

### 不在範圍內

- **呼叫前預留**（不超過上限的保證）：第二階段，設計記錄在 §16
- 跨 agent 的使用者總額度：目前只有 kyt 一個 agent，帳本保留 `agent_id`，之後要加時只是多一條查詢（§6.1）
- 臨時調高額度的審批流程：留待後續，可沿用 maker-checker
- 查詢自己剩餘額度的 API 或 `agw` 指令
- MCP 路徑的 tool 呼叫次數限制：tool 呼叫已經由 policy 與審批控管
- kyt_agent 的程式修改：另開 kyt_agent 的計畫，本文件只列出需要的改動（§12）

## 3. 結論

**採方案 B：由治理引擎在 Postgres 依（agent, 使用者）記帳。第一階段事後累計並精確記錄超額，以同時進行數與單次上限把超額限制在已知範圍；第二階段再加預留。** LiteLLM key 的預算保留為 agent 總額度。

| 項目 | 決策 | 理由 |
| ---- | ---- | ---- |
| 使用者身份 | 只取驗證過的 token 的 `sub`；client 自帶的 end user 身份一律拒絕 | `user` 等欄位由 agent 填寫，被注入的 agent 可以偽造或輪換（spike L2、L3） |
| token 的帶法 | key 放 `x-litellm-api-key`，使用者 token 放 `Authorization: Bearer` | 與 MCP 路徑相同；LiteLLM 認證時 `x-litellm-api-key` 優先（L9），hook 可從 `secret_fields` 取得原始 header（L8，待 B1 確認） |
| 預算機制 | 方案 B，不用 LiteLLM 的 end user 預算 | LiteLLM 在 guardrail 之前就用 client 自填的 id 檢查預算（L4），A 的原始做法無效；改成在 middleware 注入也拿不到依 agent 的範圍與超額紀錄（§5） |
| 判斷時機 | 第一階段事後累計：已結算的用量達到上限後，之後的請求才拒絕 | 不需要估計成本，不會「還有額度卻被擋」；實作與依賴的假設都少。預留的代價與收益見 §5.3 |
| 超額的上界 | 依等級限制同時進行的請求數、單次輸入字元數與輸出 token | 事後累計的超額等於「跨過上限時還在進行中的請求」的成本，限制這兩個因子就有上界；只靠 rpm 時被注入的 agent 可以放大到日額度的數倍（§5.3） |
| 超額的紀錄 | 結算時在同一個交易中算出這一筆超出日、月預算與 tpm 的量，寫進帳本；有超額時同一個交易寫入 `llm_limit_exceeded` 稽核 | 依使用者序列化結算，每筆的超額加總正好等於總超額（§8.4）；稽核與帳本同時成功或同時失敗 |
| 結算遺失 | 對帳指令以 `call_id` 從 `LiteLLM_SpendLogs` 補結算，走同一套超額計算 | callback 失敗或 gateway 重啟時仍能補回正確的用量與超額 |
| 範圍 | 兩層：（agent, 使用者）由 gateway 強制；agent 總額由 LiteLLM key 預算兜底 | 面試的問題是「一位使用者把 agent 的額度用光」，切開的正是這一層 |
| 週期 | 個人額度以日、月計，依 `Asia/Taipei` 的日曆重置；key 加上 `budget_duration: "1mo"` | 日額度符合分析師的工作節奏，說明時也直觀 |
| 額度來源 | OPA 依 agent 與使用者角色提供（`governance.llm` 擴充） | 與 guardrail 設定同一個來源、同樣以 GitOps 審查；policy 只回傳數值，計數由引擎做 |
| 拒絕的狀態碼 | 預算用完 403（不可重試）、rpm / tpm / 同時進行數 429（可重試）、token 問題 401 | kyt 會自動重試 429；預算要到隔天才恢復，回 429 只會浪費重試 |

**執行期驗證**（§11、[m5-findings.md](../spikes/m5-findings.md)）：B1–B6、B9、B10 已於 2026-11-13 驗證，設計不需要改變方向，細節的調整已寫回 §6.3、§8.3、§8.5。B8 與 B7 於 2026-11-14 以真實 Gemini 量測，B8 的結果已寫回 §8.6。

### 元件

| 模組 | 職責 |
| ---- | ---- |
| `engine/llm_gate.py`（新） | `LlmGate`：§4.3 的處理順序、拒絕的稽核；結算與釋放 |
| `engine/limits.py`（新） | 額度等級的資料模型；日曆期別、放行判斷與超額計算（純函式） |
| `engine/ledger.py`（新）、`engine/postgres.py` | 帳本介面；`llm_usage` 表與放行、結算、釋放的交易 |
| `engine/pricing.py`（新） | 檢查模型別名與它的 fallback 都有單價（沒有單價時 LiteLLM 的成本為 0，預算形同虛設） |
| `engine/guardrails/service.py` | 拆成 `resolve()`（agent 與 OPA 設定）與 `guard_request()`（M4 的處理） |
| `engine/policy.py` | `llm_config(agent, user)` |
| `hook_adapter.py` | 從 raw headers 取 token、偵測自帶的 end user header、從 logging 事件取出結算資料 |
| `plugin.py` | pre-call 改呼叫 `LlmGate`；success logging 事件結算；failure hook 釋放 |
| `reconcile.py`（新） | 在 gateway 容器內執行的對帳指令，讀 `LiteLLM_SpendLogs` 補結算 |

`engine/` 仍不 import LiteLLM：模型清單、fallback 與單價由 plugin 從 LiteLLM 讀出 plain data 後交給引擎。

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

M4 的處理順序加入這些步驟（粗體為新增）：

1. 只接受 `acompletion`
2. 檢查請求欄位；**拒絕 client 自帶的 end user 身份**（§4.1）
3. **驗證使用者 token**：與 MCP 路徑相同的 `TokenVerifier`（簽章、issuer、audience、到期、`azp` 在允許清單、`sub` 必填），以 `asyncio.to_thread` 執行
4. 從 key 解析 agent 與版本，向 OPA 查詢註冊狀態、guardrail 設定與**此使用者的額度**（一次查詢）
5. **檢查單次上限**：`max_tokens` / `max_completion_tokens` 沒帶就補上限、超過就拒絕；`n` 不是 1 就拒絕；請求的文字超過等級的 `max_input_chars` 就拒絕
6. 檢查大小（M4 的全域上限）
7. 注入檢查、PII 假名化、spotlighting、guardrail 稽核（M4 不變）
8. **放行判斷與記錄**：在帳本的交易中檢查同時進行數、rpm、tpm 與日、月預算，通過才寫入一筆 `in_flight`

token 放在 OPA 之前：無效 token 不必查 OPA，也避免把未驗證的 claims 送進 policy。單次上限放在 guardrail 之前：被拒絕的請求不必做 PII 偵測，也不會留下 `llm_guardrail` 稽核。放行放在最後：前面任何一步拒絕都不會留下帳本紀錄。放行之後還可能被 LiteLLM 自己的 key 限流擋下（guardrail 在 rate limiter 之前執行，M4 findings），此時由 failure hook 標為 `released`（§8.3）。

### 4.4 fail-closed

| 情況 | 結果 |
| ---- | ---- |
| 沒有 `Authorization` | 401 `missing_user_token` |
| 簽章、issuer、audience、到期錯誤，`azp` 不在允許清單（服務帳號），取不到 JWKS | 401 `invalid_user_token` |
| client 自帶 end user 身份 | 400 `end_user_override` |
| OPA 沒回應 / 回傳不完整 | 503 `policy_unavailable` / `policy_error`（沿用） |
| 使用者沒有任何符合的額度等級 | 403 `no_llm_quota` |
| 帳本資料庫連不上、`call_id` 重複、模型或它的 fallback 沒有單價 | 503 `budget_unavailable` |
| 拒絕時稽核寫入失敗 | 503 `audit_write_failed`（沿用） |

### 4.5 對 kyt_agent 的影響

kyt 的 LLM client 目前只送 virtual key（`Authorization: Bearer <key>`，見 kyt_agent `graph/deps.py` 的 `_proxy_kwargs`）。M5 上線後未帶 token 的請求一律 401，所以 kyt 要先改版（§12），以新版本 `kyt-agent@1.6` 註冊後再切換。Keycloak 的 access token 只有 300 秒（`realm-governance.json` 的 `accessTokenLifespan`），一個案件跨數分鐘，kyt 必須在每次請求前取得未過期的 token。

## 5. 方案比較

### 5.1 方案 A：交給 LiteLLM 的 end user 預算

原始構想是 pre-call hook 用 token 的 `sub` 覆寫 `user`。依 v1.105.1 的原始碼（spike L1–L7）：

| 問題 | 結果 |
| ---- | ---- |
| end user 預算存在嗎 | 存在（L1），也支援 rpm、tpm（L5）與週期重置（L7） |
| 在呼叫前檢查嗎 | 在 auth 階段檢查，早於 guardrail；但 end user id 也是在那時候、從 client 的欄位決定（L2、L4） |
| hook 覆寫 `user` 有效嗎 | **無效**：預算檢查與花費歸屬都用 auth 階段的 id，不讀 `data["user"]`（L4） |
| 與 key 預算如何疊加 | 並行檢查，任一超過就拒絕（L1）；兩者都是事後累計 |
| 跨過上限 | 花費在回應後累計，判斷式是 `>`（L1、L6），並行請求全部放行；沒有記錄超出多少 |
| fallback 會重算嗎 | 預算檢查在 auth，rate limit fallback 重跑的是 pre-call，不會重跑 auth；花費只記實際成功的那次（待 B3 確認） |

要讓 A 可行，得改成 **A'**：在 LiteLLM 認證之前加 ASGI middleware，驗證 token、移除 client 帶的所有 end user 來源（六種，L2），再補上 `x-litellm-end-user-id: <sub>`。A' 仍有這些問題：

- **拒絕與超額都不在我們的稽核裡**：LiteLLM 丟 `BudgetExceededError`，格式不是 `{"governance": "denied"}`，不寫 hash chain，也不記每筆超出多少
- **沒有超額上界**：LiteLLM 沒有依 end user 的同時進行數限制
- **依角色的額度要同步兩份資料**：OPA 的角色設定要轉成 LiteLLM 的 budget table 與每位 end user 的 `budget_id`
- **middleware 不知道 agent**：它在 key 認證之前執行，額度只能依使用者，不能依（agent, 使用者）
- **依賴 LiteLLM 內部優先序**：六種來源的優先序是實作細節（L2），升級時改變就會被繞過
- 沒有 Redis 時，`spend:end_user:*` 計數在各 worker 的記憶體，多 worker 時不準

### 5.2 方案 B：治理引擎記帳（採用）

放行時記一筆、回應後以 LiteLLM 算出的實際成本結算，超額在結算時計算並寫稽核。細節見 §8。B 的程式比較多，但每一項都在我們能測試的範圍內，也和既有的治理一致：身份來自 token、額度來自 OPA、拒絕與超額寫 hash chain、無法判斷就拒絕。

### 5.3 事後累計與預留

B 可以在呼叫前預留最壞成本（§16），也可以事後累計。用 gateway 的 `LiteLLM_SpendLogs` 實測（2026-11-13，195 筆成功的 `gemini/gemini-3.8-flash` 呼叫）：

| | 中位數 | p95 | 最大 |
| - | ------ | --- | ---- |
| 輸入 token | 3,120 | 6,602 | 10,919 |
| 輸出 token | 350 | 1,385 | 1,806 |
| 成本 | 約 $0.0037 | | $0.014（平均 $0.0043） |

| 面向 | 事後累計（第一階段） | 預留（第二階段） |
| ---- | -------------------- | ---------------- |
| 超過上限的量 | 跨過上限時進行中的請求全數放行；有同時進行數與單次上限時，上界見 §8.6 | 估計不低於實際時不會超過 |
| 還有額度卻被擋 | 不會 | 剩餘不足一筆預留時被擋；輸出上限 8,192 時預留約為實際的 10 倍（中位數），tpm 在並行時會過早擋下 |
| 需要估計成本 | 不用 | 需要價格表、輸入估計（B8）與 reasoning 是否計入上限（B7） |
| 實作量 | 帳本、結算、超額計算、對帳 | 再加上估計、預留與過期處理 |

只靠 rpm 時，事後累計的超額上界是「rpm × 單筆最壞成本」。M4 的請求上限是 100 萬字元，單筆最壞約 $0.76，個人 rpm 20 時被注入的 agent 一分鐘內最多超出約 $15，是日額度的 7 倍。因此第一階段一定要搭配同時進行數與單次輸入、輸出上限（§7），把上界壓到日額度的兩成以內，正常的 kyt 使用則只會超出最後一筆（實測最多 $0.014）。

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
- 以放行的時間（`created_at`）決定屬於哪一期：跨過午夜的請求算在開始那天，判斷與結算看的是同一期
- 拒絕的回應帶 `resets_at`，讓 agent 與使用者知道何時恢復

### 6.3 key 的總額度

目前 `max_budget: 5.0` 沒有週期，用完就永久停擺。改為 `budget_duration: "1mo"`，`max_budget` 依「預期使用者數 × 個人月額度」設定，示範環境用 30.0。`"1mo"` 由 LiteLLM 的 reset job 處理，在每月 1 日 00:00 UTC（台北 08:00）重置（B9）；個人月額度則在台北時間的 1 日 00:00 重置，兩者差 8 小時。

## 7. 依使用者的流量與單次上限

| 限制 | 判斷方式 | 用途 |
| ---- | -------- | ---- |
| `max_concurrent` | 狀態為 `in_flight`、且放行未超過 `GOV_USAGE_INFLIGHT_TTL_SECONDS`（預設 300）的請求數 | 超額上界的第一個因子；kyt 一次一筆，設 4 留餘裕 |
| `rpm` | 過去 60 秒內放行的請求數（不論結果） | 精確的事前檢查，不會超過 |
| `tpm` | 過去 60 秒內**結算**的實際 token（輸入 + 輸出） | 事後累計，與預算相同；超出量在結算時記錄 |
| `max_output_tokens` | 沒帶 `max_tokens` / `max_completion_tokens` 就補上限；任一個超過就 400 `max_tokens_exceeded` | 超額上界的第二個因子；沒有上限時 Gemini 單次最多 65,536 個輸出 token（L10） |
| `n` | 必須沒帶或剛好是 1，否則 400 `unsupported_field` | 一筆請求會產生 n 個回應、成本是單次最大值的 n 倍，§8.6 的上界就不成立 |
| `max_input_chars` | 送給 guardrail 之前，訊息與 tool 參數的文字總長超過就 400 `content_too_large` | 超額上界的第三個因子；M4 的全域上限 100 萬字元太寬 |

- rpm 只計放行的請求，被拒絕的不計，否則持續送請求會一直鎖住自己
- 補上的 `max_tokens` 與 M4 一樣在 pre-call 改寫 `data`，fallback 沿用（B10）
- 輸出上限截斷會讓 agent 收到不完整的回應，所以超過上限時拒絕而不截斷；補上的上限要依實測設定：kyt 的輸出最大 1,806 token，設 4,096
- `max_input_chars` 依實測設定：kyt 的輸入最大 10,919 token，設 100,000 字元
- key 的 `tpm_limit` 與 `rpm_limit` 保留，作為 agent 的總量。個人 tpm 應明顯低於 key 的 tpm（示範值 150,000 對 300,000）

## 8. 帳本、結算與超額

### 8.1 帳本

`governance` 資料庫新增一張表：

```sql
CREATE TABLE IF NOT EXISTS llm_usage (
    owner text NOT NULL,                -- key 的雜湊，與 PendingPseudonyms 相同（owner_of）
    call_id text NOT NULL,              -- litellm_call_id，可對照 LiteLLM_SpendLogs
    agent_id text NOT NULL,
    user_sub text NOT NULL,
    model text NOT NULL,
    tier text NOT NULL,
    limits json NOT NULL,               -- 放行時的額度快照，結算用它計算超額
    status text NOT NULL,               -- in_flight | settled | released
    created_at timestamptz NOT NULL,
    settled_at timestamptz,
    settled_by text,                    -- callback | reconcile
    cost_usd numeric,
    prompt_tokens integer,
    completion_tokens integer,
    over_day_usd numeric,               -- 這一筆超出日預算的金額（0 表示沒有超出）
    over_month_usd numeric,
    over_tpm_tokens integer,            -- 這一筆結算時超出 tpm 的 token
    PRIMARY KEY (owner, call_id)
);
CREATE INDEX IF NOT EXISTS llm_usage_user ON llm_usage (agent_id, user_sub, created_at);
CREATE INDEX IF NOT EXISTS llm_usage_settled ON llm_usage (agent_id, user_sub, settled_at);
```

- 帳本不進 hash chain：它是計量紀錄，每次 LLM 呼叫都寫 hash chain 會被全域鎖序列化。治理決策（拒絕、超額）才寫 hash chain
- 主鍵含 `owner`：`call_id` 可由 client 以 `x-litellm-call-id` 指定，不同 key 可能重複（M4 的對照表也是這樣處理）。同一把 key 重複使用 `call_id` 時插入失敗，回 503 `budget_unavailable`
- 額度存放行時的快照：等級在請求進行中改變時，結算仍以放行時的額度計算，紀錄才解釋得通
- 不記 PII：`user_sub` 是 Keycloak 的 UUID，與 `approvals`、`audit_events` 相同；不記訊息內容

### 8.2 放行

一個交易內完成：

```sql
SELECT pg_advisory_xact_lock(72410002, hashtext(agent_id || ':' || user_sub));
-- 依序檢查：同時進行數、rpm、tpm（過去 60 秒結算的 token）、本日與本月已結算的成本
-- 全部未達上限才 INSERT 一筆 in_flight
```

- 預算判斷式是「已結算 ≥ 上限就拒絕」：剛好用到上限時之後的請求就擋
- 只看已結算的金額，進行中的請求不計，所以不會「還有額度卻被擋」
- 以（agent, 使用者）序列化，不同使用者互不等待。使用雙參數的 advisory lock，與稽核的 `AUDIT_LOCK_KEY` 不同命名空間
- 拒絕的稽核在這個交易之外寫入

### 8.3 結算的時機

| 事件 | 動作 |
| ---- | ---- |
| `async_log_success_event`（LiteLLM 計費完成，含 streaming 結束） | 結算：寫入成本、token 與超額（§8.4）；`settled_by = callback` |
| `async_post_call_failure_hook`（請求最終失敗，含被 LiteLLM 限流擋下、自己的 hook 拒絕） | 只把 `in_flight` 改為 `released`；已 `settled` 的不動 |
| 都沒有收到 | 保持 `in_flight`；超過 `GOV_USAGE_INFLIGHT_TTL_SECONDS` 後不再佔同時進行數；由對帳補結算（§8.5） |

- 結算不論原狀態都更新（已 `released` 的也一樣），實際有花費就要記；已 `settled` 的重複事件不重算
- 不用 `async_log_failure_event` 釋放：router 重試與 fallback 前的每次失敗可能都會觸發（B4），那時請求還在進行
- `async_post_call_success_hook` 不用於結算：streaming 時不會呼叫（M4 findings A4），也拿不到成本
- **router fallback**：同一個 `call_id`（A6），成功的那次結算實際成本，帳本記的 `model` 是請求的別名、成本是實際模型的
- **rate limit 觸發的 fallback**：proxy 以新的 `call_id` 重跑 pre-call，第二筆收不到任何事件（B5 已確認），由對帳標為 `released`；key 的限流造成的這種紀錄見 §17.2
- **streaming**：結算在串流結束後的 success 事件（B3）。client 中途斷線時仍有 success 事件，成本只含斷線前產生的部分（B6）；真實 provider 斷線後可能已產生並計費更多輸出，帳本會少記，列為已知限制
- 成本是有二進位誤差的 float（B3），先四捨五入到小數第 12 位再轉 `Decimal`，與 `LiteLLM_SpendLogs.spend` 一致；call_id 取 `kwargs["litellm_call_id"]`，`standard_logging_object["id"]` 不是 call_id

### 8.4 超額的計算

結算在一個交易中完成，與放行用同一把（agent, 使用者）的鎖：

1. 讀出這一筆所屬的日、月期別中**其他已結算**請求的成本總和 `before`；期別以這一筆的放行時間決定，上下界都要比對（23:59 放行、午夜後才結算的請求仍算前一天）
2. `after = before + cost`
3. 這一筆的超額 `over = max(0, after − max(before, limit))`，日、月各算一次，`limit` 取這一筆放行時的快照
4. tpm 同理：視窗為結算時間往前 60 秒，`before` 為視窗內其他已結算請求的 token，`limit` 為快照的 `tpm`。結算時間在等鎖之前取得，取得鎖後若早於這位使用者最新的 `settled_at`，就調成那個時間：並行結算時後拿到鎖的那筆可能帶著較早的時間，視窗會漏掉剛結算的那筆，兩筆互相看不到，tpm 的超額就少算。這個調整只用於 callback；對帳補結算的是舊請求，保留它真實的結束時間（§8.5）
5. 更新這一筆的 `status`、成本、token 與三個超額欄位
6. 任一個超額大於 0 時，**在同一個交易中**寫入 `llm_limit_exceeded` 稽核（§10.2）

性質：

- **加總正確**：結算以（agent, 使用者）序列化、結算時間不早於前一筆，同一期內各筆的 `over` 依結算順序相加，正好等於 `max(0, 期別總成本 − limit)`，不會重複計算，也不會漏算。例如日額度 $2.00，已結算 $1.99，兩筆並行的請求分別花 $0.01 與 $0.012：先結算的那筆 `over = 0`（剛好到 $2.00），後結算的 `over = 0.012`，總超額 $0.012
- **跨過上限的那一筆可以找出來**：`over > 0` 且 `before < limit` 的就是跨過上限的請求；`before ≥ limit` 的是跨過之後才結算的並行請求
- **稽核與帳本一致**：同一個交易，兩者同時成功或同時失敗。鎖的順序固定為「使用者鎖 → 稽核鎖」，放行與拒絕的稽核不會在持有使用者鎖時取稽核鎖，因此不會死鎖
- 等級在同一期中改變時，各筆以自己的快照計算，加總不再等於「總成本減某一個上限」，但每一筆的超額仍有明確定義

### 8.5 對帳

結算事件可能遺失（gateway 在回應後、結算前重啟，或結算交易失敗），rate limit fallback 也會留下收不到事件的紀錄。對帳指令在 gateway 容器內執行（容器有兩個資料庫的連線設定）：

```bash
docker compose exec gateway python -m ai_gov_gateway.reconcile --older-than 10m
```

- 找出 `in_flight` 且放行超過指定時間的紀錄，以 `(sha256(api_key), litellm_call_id)` 對照 `LiteLLM_SpendLogs`
- 有 `status = success` 的紀錄：以它的 `spend`、`prompt_tokens`、`completion_tokens` 結算，走 §8.4 同一套計算，`settled_by = reconcile`。結算時間取該紀錄的 `endTime`（UTC），舊用量才不會落進目前的 tpm 視窗而造成假的 429 或假超額，也不套用鎖順序的調整；沒有 `endTime` 時退回對帳當下的時間
- 只有失敗紀錄：標為 `released`
- 都沒有：放行未超過 `--release-missing-after`（預設 1 小時）時保留 `in_flight`，等 LiteLLM 的批次寫入；超過後標為 `released`，在輸出中分開計數（`released_missing`）。rate limit fallback 的第二個 `call_id` 在 SpendLogs 沒有任何紀錄（B5），不這樣處理會累積永遠無法解決的紀錄
- 對帳是補救，不是主要路徑；輸出的筆數可以當作 callback 遺失的監控指標

### 8.6 超額的上界

事後累計的超額來自「跨過上限時還在進行中的請求」：

```
同一期的超額 ≤ max_concurrent × 單筆最大成本
單筆最大成本 = max_input_chars × 最高輸入單價 + max_output_tokens × 最高輸出單價
```

kyt 的設定（`max_concurrent` 4、`max_input_chars` 100,000、`max_output_tokens` 4,096、Gemini 單價 $0.75 / $3.75 每百萬 token）：B8 實測 kyt 的內容 token 數最多是字元數的 1.02 倍，輸入最多約 102,000 token。單筆最大約 $0.0765 + $0.0154 ≈ $0.092，超額上界約 $0.37，是日額度 $2 的 18%。正常的 kyt 使用一次一筆，只會超出最後一筆，實測最多 $0.014。

1.02 只代表 kyt 的內容（hex 地址、交易對手清單、中文說明）；英文為主的內容通常是 3–4 字元／token，上界更寬鬆。新的 agent 若內容的 token 比字元多，要重新量測後調整 `max_input_chars`。

逾時未結算的請求不再佔同時進行數（§7），它的成本如果之後才結算，會讓實際超額大於上界；這類情況只有結算延遲超過 300 秒才會發生，由對帳補記。

## 9. OPA policy

額度放在 `policies/agents/data.json` 各 agent 的 `llm_limits`，依序比對等級，第一個符合使用者角色的生效（`"*"` 表示任何驗證過的使用者）：

```json
"llm_limits": [
  {
    "tier": "supervisor",
    "role": "compliance_supervisor",
    "budget_usd": {"day": 5.0, "month": 50.0},
    "rpm": 30,
    "tpm": 150000,
    "max_concurrent": 4,
    "max_input_chars": 100000,
    "max_output_tokens": 4096
  },
  {
    "tier": "default",
    "role": "*",
    "budget_usd": {"day": 2.0, "month": 20.0},
    "rpm": 20,
    "tpm": 150000,
    "max_concurrent": 4,
    "max_input_chars": 100000,
    "max_output_tokens": 4096
  }
]
```

- 用有序清單而不是「預設加角色覆寫」：每個等級的值都是完整的，不需要合併規則，審查時一眼就看得出某位使用者會套用哪些數字
- 已註冊但沒有 `llm_limits` 的 agent 套用最嚴格的預設（與 guardrail 的 `strict_guardrails` 相同的做法），例如 `day: 0.5`、`rpm: 10`、`tpm: 50000`、`max_concurrent: 1`、`max_input_chars: 50000`、`max_output_tokens: 2048`
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
| 400 | `max_tokens_exceeded` | `max_tokens` 或 `max_completion_tokens` 超過等級的上限，或不是正整數 | `field` |
| 400 | `unsupported_field` | 請求的 `n` 不是 1；多個回應會讓單筆成本超過 §8.6 的上界 | `field` |
| 400 | `content_too_large` | 文字總長超過等級的 `max_input_chars`（沿用 M4 的代碼） | |
| 403 | `no_llm_quota` | 沒有符合使用者角色的額度等級 | |
| 403 | `user_budget_exceeded` | 本日或本月已結算的成本達到上限 | `limit: {"type": "budget_day", "resets_at": "…"}` |
| 429 | `user_rate_limited` | 同時進行數、rpm 或 tpm 達到上限 | `limit: {"type": "rpm", "retry_after_seconds": 12}` |
| 503 | `budget_unavailable` | 帳本無法讀寫、`call_id` 重複、模型或 fallback 沒有單價 | |

```json
{"governance": "denied", "reasons": ["user_budget_exceeded"], "limit": {"type": "budget_day", "resets_at": "2026-11-14T00:00:00+08:00"}}
```

### 10.2 稽核事件

**`llm_access`**：M5 新增的每一次拒絕（上表除了 503 `budget_unavailable` 之外），以及 M4 的版本未註冊、agent 停用（補上 M4 的已知限制）：

| 欄位 | 內容 |
| ---- | ---- |
| `agent_id`、`agent_version`、`key_alias` | 呼叫的 agent 與 key（key 名稱格式不符時為 `null`） |
| `user_sub`、`username` | 驗證過的使用者；token 無效時為 `null` |
| `decision`、`reasons` | `deny` 與錯誤代碼 |
| `policy_version` | 有查到 OPA 時填入 |
| `details` | `call_id`、`model`、`tier`、`limit`（種類、恢復時間與設定的上限 `max`，例如 `{"type": "rpm", "retry_after_seconds": 12, "max": 20}`），以及判斷時的用量，例如 `{"settled_usd": "2.004"}` 或 `{"requests": 20}` |

**`llm_limit_exceeded`**：結算時任一項超額大於 0，與帳本同一個交易寫入：

| 欄位 | 內容 |
| ---- | ---- |
| `agent_id`、`key_alias`、`user_sub` | 放行時的 agent、key 與使用者（帳本的值） |
| `decision` | `over_limit` |
| `reasons` | 超出的項目，例如 `["budget_day", "budget_month"]` |
| `details.call_id`、`details.model`、`details.tier` | 對照帳本與 `LiteLLM_SpendLogs` |
| `details.cost_usd`、`details.tokens` | 這一筆的實際成本與 token（輸入、輸出分開） |
| `details.over` | 每一項的 `limit`、`before`、`after`、`over`，例如 `{"budget_day": {"limit": "2.0", "before": "1.99", "after": "2.002", "over": "0.002"}}` |
| `details.crossed` | 這一筆是否就是跨過上限的那一筆（`before < limit`） |
| `details.settled_by` | `callback` 或 `reconcile` |

- 金額以字串記錄，避免 JSON 的數值表示讓 hash 不一致
- 不記 token 原文、訊息內容與 PII 原值；金額與 token 數不是個資
- 放行與正常結算不寫稽核，計量紀錄在帳本，以 `call_id` 對照

## 11. 尚未驗證的假設

靜態檢查（spike L1–L10）已確認方案 A 的限制與 B 需要的入口存在。執行期驗證的結果見 [m5-findings.md](../spikes/m5-findings.md)：B1–B3、B5、B9、B10 成立；B4 只觸發一次 `log_failure`，不影響設計；B6 有 success 事件但只含斷線前的用量。B8 不成立但差距小（kyt 的內容約 1 token／字元，最多 1.02），§8.6 的上界已乘上 1.02；B7 成立（Gemini 的 `max_tokens` 涵蓋 reasoning token）。

| 編號 | 假設 | 影響 | 不成立時 |
| ---- | ---- | ---- | -------- |
| B1、B2 | hook 從 `secret_fields.raw_headers` 取得 `Authorization`，LiteLLM 以 `x-litellm-api-key` 認證、不轉送 JWT | §4.2 token 的帶法 | 改用 `x-gov-user-token` + middleware |
| B3 | guardrail 會收到 `async_log_success_event`，帶相同 `call_id`、實際成本與 token，streaming 與 fallback 各一次 | §8.3 結算 | 另註冊一個結算用的 `CustomLogger` |
| B4 | router 重試的每次失敗都會觸發 `async_log_failure_event` | §8.3 不用它釋放 | 無影響 |
| B5 | rate limit fallback 的第二個 `call_id` 收不到事件 | §8.3、§8.5 | 改由該事件釋放 |
| B6 | streaming 中斷時的事件與成本 | §8.3 | 由對帳處理，列為已知限制 |
| B8 | 字元數不低於 token 數 | §8.6 的上界（不影響判斷） | 上界乘上實測係數 |
| B9 | key 接受 `budget_duration: "1mo"` | §6.3 | 改用 `"30d"` |
| B10 | 改寫的 `max_tokens` 會送到 provider，fallback 沿用 | §7 | 只拒絕、不補值 |

B7（Gemini 的 `max_tokens` 是否涵蓋 reasoning token）只有預留需要，移到第二階段。

## 12. kyt_agent 需要的改動（另開計畫）

- LLM client 改為送 `x-litellm-api-key: Bearer <key>` 與 `Authorization: Bearer <使用者 token>`。以 `httpx.Auth` 在每次請求前取得 token，到期前 30 秒更新，傳給 `init_chat_model` 的 `http_client` / `http_async_client`
- 取得 token 的程式與 `GatewayExecutor._token()` 共用（同一個使用者、同一個 `kyt-cli` client）
- 錯誤處理：403 `user_budget_exceeded` 停止此案件並在報告與 audit log 寫明「額度已用完」與 `resets_at`；429 `user_rate_limited` 依 `retry_after_seconds` 等待後重試；400 `max_tokens_exceeded`、`content_too_large` 視為設定錯誤
- 不送 `user` 欄位（目前已經沒有送）
- 以新版本 `kyt-agent@1.6` 註冊；eval 改用專用的測試使用者與等級，避免批次 eval 用光分析師的日額度
- 未啟用治理（直連 Gemini）時行為不變

## 13. 測試與驗收

- **單元測試**：token 取出與 end user 欄位檢查（`hook_adapter`、`fields`）、額度等級的模型驗證、單價檢查（`pricing`）、放行判斷與超額計算（純函式，含期別邊界、時區、並行結算的加總、等級快照）、`LlmGate` 的順序與稽核
- **Rego 測試**：等級的順序與 `"*"`、沒有 `llm_limits` 的嚴格預設、沒有符合等級時為 `null`、欄位不完整時 `llm` 為 undefined
- **整合測試**（`governance_test` 資料庫）：放行、結算、釋放；20 筆並行結算後，各筆 `over` 的加總等於總成本減上限；超額與稽核在同一個交易（稽核寫入失敗時帳本也不變）；跨午夜與跨月的期別；對帳以假的 SpendLogs 資料補結算
- **契約測試**（gateway + probe）：B1–B6、B10 的行為固定成測試；拒絕的狀態碼與 `provider_specific_fields`；帳本的成本與 token 等於 `LiteLLM_SpendLogs`；並行跨過上限時 `over` 的加總正確
- **端到端示範** `scripts/demo_m5.py`：缺 token、無效 token、服務帳號 token、自帶 `user`、自帶 header、`max_tokens` 過大、補上 `max_tokens`、低額度使用者跨過預算（記錄超額）後被擋、其他使用者不受影響、並行跨過上限的超額加總、同時進行數、rpm、streaming 結算、fallback 結算、對帳
- **真實 LLM**：以真實 Gemini 送 2 次，帳本的成本與 `LiteLLM_SpendLogs` 一致（B3 的真實單價版本）；成本 < $0.01
- **完成標準**：上述全部通過；`opa test` 通過；`uv run pytest -m integration` 全數 passed（不是 skipped）；`demo_m4` 改為帶 token 後仍全數通過

## 14. 風險

| 風險 | 緩解 |
| ---- | ---- |
| 事後累計讓最後一批請求超過上限 | 同時進行數與單次上限讓超額有上界（§8.6）；每一筆的超額都記錄並寫稽核；需要硬上限時做第二階段（§16） |
| LLM 路徑多了 token 驗證與兩次 Postgres 交易（放行、結算），延遲增加 | JWKS 有快取；交易只鎖單一使用者；結算在回應之後；在契約測試記錄延遲，與 M4 比較 |
| Postgres 停擺時所有 LLM 流量失敗 | fail-closed 的必然結果，與 OPA 相同，列入已知限制 |
| 結算事件遺失造成少算 | 對帳指令；對帳列出的筆數作為監控指標 |
| 模型沒有單價時成本為 0，預算形同虛設 | 放行前檢查別名與所有 fallback 都有單價，否則 503；契約測試確認 `kyt-main` 的成本大於 0 |
| 被注入的 agent 大量送出超額請求，每次都寫 hash chain | 拒絕不經過模型，成本只有稽核寫入；必要時再加「同一使用者、同一原因 60 秒內只寫一筆並記數量」 |
| LiteLLM 升級改變 `secret_fields`、結算事件或 hook 順序 | 契約測試涵蓋，升級前必跑（AGENTS.md 既有規定） |
| 日額度讓長時間的調查中途停止 | 拒絕回 `resets_at`；kyt 在報告寫明原因；等級由 GitOps 調整 |

## 15. 要更新的文件

- `README.md`：設計原則表加入「依使用者的額度」；功能清單；LLM 路徑的處理順序（§4.3）；錯誤代碼表與 `llm_access`、`llm_limit_exceeded` 稽核事件；「用量、成本與稽核紀錄」加入 `llm_usage` 與超額欄位；`GOV_BUDGET_*`、`GOV_USAGE_*` 環境變數；「fallback 與 tpm」改寫為兩層額度；對帳指令；已知限制（§8.6、§14）；`probe_llm` 有測試單價；開發進度加入 M5
- `AGENTS.md`：開發階段表加入 M5；目錄結構加入帳本、價格檢查與對帳；升級 LiteLLM 前要跑的測試數量；安全段落加入「LLM 路徑的身份只來自 token，client 自帶的 end user 身份一律拒絕」
- `docs/images/architecture.svg`：LLM 流量加入「使用者身份」與「依使用者的額度」兩個能力，標為 M5
- 本 spec 實作後加上「實作後修訂」一節；spike 結果寫入 `docs/spikes/m5-findings.md`
- workspace 的 `AGENTS.md` 專案索引與 `docs/ai-gov-gateway/` 學習文件

## 16. 第二階段：呼叫前預留（記錄，不在本次實作）

需要「不超過上限」的硬保證時，在第一階段的帳本上加預留。帳本結構相容：多一個 `reserved` 狀態與預留金額、token 欄位，結算與超額計算不變。

### 16.1 做法

放行時估計最壞成本並預留，「已結算 + 未結算的預留 + 這一筆的估計」不超過上限才放行：

```
estimate_tokens = input_estimate + max_output_tokens
estimate_usd    = input_estimate × 最高輸入單價 + max_output_tokens × 最高輸出單價
```

- **輸入估計**：請求中 `messages` 與 `tools` 的 JSON 字元數，或 `litellm.token_counter` 乘上安全係數
- **單價**：模型與所有 fallback 中最高的，來自 LiteLLM 的 `model_cost` 或 deployment 的 `model_info`
- **保證**：估計不低於實際時，任何時候「已結算 + 未結算預留」都不超過上限，最後一筆與並行請求都不會超過
- 預留在結算或釋放時解除；收不到事件的預留（rate limit fallback）在 TTL 後不再佔用額度

### 16.2 實測的代價

以 §5.3 的實測與「約 3 字元／token」（B8 未驗證）估算：

| 情況 | 預留 | 實際 | 倍數 |
| ---- | ---- | ---- | ---- |
| 中位數那一筆，輸出上限 8,192 | $0.038 | $0.0037 | 約 10 倍 |
| 最大那一筆，輸出上限 8,192 | $0.055 | $0.014 | 約 4 倍 |
| 中位數，輸出上限 2,048 | $0.015 | $0.0037 | 約 4 倍 |

- 預算：剩餘額度不足一筆預留時就擋，日額度 $2 時最多少用 1.6–2.8%
- **tpm 會過早擋下**：每筆進行中的請求預留約 17,600 token（輸入字元數 + 8,192），實際約 3,500；個人 tpm 150,000 時約 8 筆並行就會被擋。第二階段的 tpm 應只以輸入估計加 p95 輸出量計算，或改由預算與 key 的 tpm 把關
- 需要額外驗證 B7（Gemini 的 `max_tokens` 是否涵蓋 reasoning token，不涵蓋時預留要另加 reasoning 上限）與 B8

### 16.3 何時做

- 第一階段的 `llm_limit_exceeded` 稽核顯示超額經常發生、或金額接近 §8.6 的上界
- 新的 agent 單筆成本高（例如長上下文），事後累計的超額上界超過可接受的範圍
- 合規或財務要求「不得超過」的硬上限

## 17. 實作後修訂

第一階段依 [實作計畫](../plans/2026-11-13-user-budget.md) 完成（Task 1–14）。與正文不同、或正文沒寫到的地方如下；正文已同步修改的項目在「原因」註明。

### 17.1 設計與行為

| 項目 | 原本 | 實作 | 原因 |
| ---- | ---- | ---- | ---- |
| spike 的環境 | 修改 M4 的 `litellm/config.spike.yaml` | 另建 `litellm/config.spike-m5.yaml`、`docker-compose.spike-m5.yml`；`probe_llm` 新增 `GET /request-headers`（只記 header 名稱與是否為 JWT）與 `probe:chunk_delay` | 保留 M4 的重現環境；B2、B6 的契約測試也要用到這兩個 probe 功能 |
| key 的 `"1mo"`（B9） | 從建立時間起算 | 每月 1 日 00:00 UTC（台北 08:00）重置，與個人月額度差 8 小時 | B9 實測的 `budget_reset_at`；§6.3 已改 |
| 對帳：查不到 SpendLogs | 保留 `in_flight` | 放行超過 `--release-missing-after`（預設 1h）仍查不到時標為 `released`，輸出中分開計數為 `released_missing` | rate limit fallback 的第二個 `call_id` 永遠沒有 SpendLogs（B5）；§8.5 已改 |
| 對帳：被拒絕的請求 | 未確認被拒絕的請求是否寫入 SpendLogs | 被拒絕的請求（含 gateway hook 拒絕的）在 `LiteLLM_SpendLogs` 記為 `failure`、`spend` 為 0；對帳只以 `success` 結算，只有失敗紀錄的一律釋放，不會誤記成本 | 契約測試 `test_end_user_overrides_rejected` 的實際紀錄 |
| 結算的 `call_id` | 未指定來源 | 取 `kwargs["litellm_call_id"]`；`standard_logging_object["id"]` 不是 call_id | B3；§8.3 已改 |
| 結算的成本 | `Decimal(response_cost)` | `round(value, 12)` 後再轉 `Decimal`；對帳的 `spend` 也一樣處理 | `response_cost` 有二進位誤差（B3），四捨五入後與 `SpendLogs.spend` 相同；§8.3 已改 |
| 結算時間 | 結算事件的時間 | 取得使用者鎖後，若早於這位使用者最新的 `settled_at` 就調成那個時間（`_in_lock_order`） | 並行結算時後拿到鎖的那筆可能帶著較早的時間，兩筆的 tpm 視窗互相看不到，超額少算（`fd12710`）；§8.4 已改 |
| 對帳的結算時間 | 對帳當下的時間 | 讀 `LiteLLM_SpendLogs` 的 `endTime` 當結算時間，且 `_in_lock_order` 只套用在 callback 的結算；沒有 `endTime` 時退回當下 | 以當下時間結算會讓舊 token 落進目前 60 秒的 tpm 視窗，造成假的 429 與假的 tpm 超額；§8.4、§8.5 已改 |
| 結算的期別 | 期別的起點 | 以放行時間求出期別的 `[start, end)`，`before` 同時比對上下界（`period_bounds`） | 23:59 放行、午夜後才結算的請求，若只比對起點會把隔天已結算的成本也算進 `before` |
| 帳本欄位 | 沒有 `key_alias` | `llm_usage` 多一欄 `key_alias`，`llm_limit_exceeded` 稽核的 `key_alias` 取自帳本 | 結算事件拿不到 key 名稱，稽核要能對照是哪個版本（`0f2ecb0`） |
| `n` | 沒有限制 | 必須沒帶或剛好是 1，否則 400 `unsupported_field`（`field: "n"`），並寫 `llm_access` | 審查時發現 `n` 個回應會讓單筆成本變成 n 倍，§8.6 的上界不成立（`70a556e`）；§7、§10.1 已改 |
| `llm_access` 的 `details.limit` | `type` 與恢復時間 | 同時進行數、rpm、tpm 與單次上限的拒絕另含設定的上限 `max`，並列判斷時的用量（`in_flight`、`requests`、`tokens`、`chars`）；預算的拒絕另記 `settled_usd` 與 `limit_usd`。回給 client 的 `limit` 不含 `max` | 稽核要看得出是哪個上限擋下；§10.2 已改 |
| `llm_limit_exceeded` 的 `details.crossed` | 一個布林值 | 依項目記錄，例如 `{"budget_day": true, "tpm": false}`，只列超額大於 0 的項目 | 同一筆可能跨過日額度、但 tpm 早已超過，單一布林說不清楚 |
| failure hook 的釋放 | 收到 failure 就釋放 | `AdmittedCalls` 記下這個 worker 放行過的 `(owner, call_id)`，只釋放其中的請求；沿用進行中 `call_id` 的請求被拒絕時記一次，先抵銷它觸發的 failure | client 可用 `x-litellm-call-id` 沿用進行中的 `call_id`，被拒絕的那個請求也會觸發 failure hook，原本會把原請求的帳本標為 `released`、並丟掉它的 PII 對照表。後者是 M4 就有的問題，一起修正（`534a0e8`） |
| `LlmGuardrail` | 一個 `guard_request(key_alias, …)` | 拆成 `resolve(key_alias, user, model, call_id)`（agent 與 OPA 設定）與 `guard_request(agent, config, …)`；`llm_guardrail` 稽核的 `key_alias` 取自 `agent.key_alias` | `LlmGate` 要在 OPA 之後、guardrail 之前檢查額度與單次上限 |
| 未註冊與停用的稽核 | M4 不寫稽核 | `resolve()` 拒絕時寫 `llm_access`（`unregistered_agent_version`、`agent_suspended`） | 補上 M4 的已知限制（§10.2） |
| `LlmGate` 的介面 | 結算與釋放沒有回傳值 | `settle()`、`release()` 回傳是否寫入帳本，對帳以此計數；`Services.llm_gate` 取代 `llm_guardrail`；單價來源由 plugin 以 `runtime.use_pricing()` 交給 runtime | `engine/` 與 `runtime` 不 import LiteLLM；對帳要分得出「已結算」與「沒有可結算的紀錄」 |
| B8 與超額上界 | 假設字元數不低於 token 數，上界約 $0.36 | kyt 的內容約 1 token／字元（0.98–1.00 字元／token，token 最多為字元的 1.02 倍），單筆最大約 $0.092，上界約 $0.37 | 真實 Gemini 實測（findings「真實 LLM 驗證」）；§8.6 已改 |
| B7（第二階段） | 未驗證 | Gemini 的 `max_tokens` 涵蓋 reasoning token：`max_tokens=256` 時 `completion_tokens` 252，其中 reasoning 245 | 順帶量測；§16 的預留不需要另加 reasoning 上限 |
| 測試用等級 | §9 只有 `supervisor` 與 `default` | `kyt-agent` 的第一個等級是 `quota_probe`（角色 `llm_quota_probe`，日額度 $0.002、輸出上限 64），Keycloak 新增只有這個角色的測試帳號 `erin` | 契約測試與 `demo_m5` 要在幾筆 probe 請求內跨過預算，不影響 alice、bob、carol 的額度 |

### 17.2 實作時發現的已知限制

| 限制 | 影響 | 處理 |
| ---- | ---- | ---- |
| LiteLLM 以 **key** 的 rpm / tpm 擋下有 fallback 的模型時，以新的 `call_id` 重跑 pre-call | gateway 放行了第二個 `call_id`，但它收不到任何結算或失敗事件：在 `GOV_USAGE_INFLIGHT_TTL_SECONDS`（300 秒）內佔一個 `max_concurrent`，60 秒內計入 rpm；對帳在 1 小時後標為 `released`（`released_missing`），成本為 0 | 第一階段只記錄，不修正。只在 key 的總量被打滿時發生，影響是暫時少一個同時進行數；修正需要分辨 LiteLLM 的重跑，依賴更多內部行為。契約測試的限流探測改用沒有 fallback 的 `probe-fallback`，避免留下這種紀錄 |
| 有單價的模型別名在每個 process 第一次使用時建立後快取 | 以 LiteLLM 的 API 新增模型或 fallback 後，新的別名在重啟前一律 503 `budget_unavailable` | 新增模型後重啟 gateway；fail-closed，不會放行沒有單價的模型 |
| 沿用進行中 `call_id` 的請求被拒絕，但它的 failure hook 沒有觸發 | 抵銷的次數留在 `AdmittedCalls`，原請求之後真的失敗時不會被釋放，要等 300 秒的 TTL 或對帳 | fail-closed，只影響自己（重複使用 `call_id` 的同一把 key）的同時進行數 |
| `max_input_chars`、`text_length` 只計 `messages` 的文字 | 不含 `tools` 的 schema 與 `image_url`，§8.6 的上界以只有文字內容為前提；大量的 tools 定義或圖片會讓實際輸入成本高於上界 | 只記錄；kyt 目前只送文字，需要時再把這兩項納入計算 |
| streaming 中途斷線（B6） | 帳本只記到斷線前的用量；真實 provider 斷線後可能已產生並計費更多輸出 | 列入 README 已知限制 |
