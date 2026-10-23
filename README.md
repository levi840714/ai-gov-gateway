# AI Governance Gateway

**AI agent 要替人執行高風險動作時，先確認身份、套用 policy，必要時等人核准。**

AI Governance Gateway 是 agent 存取 LLM 與工具的統一入口。LLM 和 MCP 流量交給 [LiteLLM](https://github.com/BerriAI/litellm) 處理，治理邏輯則跑在同一個 process 裡：每次 tool 呼叫都會檢查是哪個 agent、代表哪位使用者、這個動作能不能做、要不要主管核准，決策結果全部寫進防竄改的稽核紀錄。

一次需要核准的呼叫大致是這樣：

```
agent 呼叫 freeze_account，凍結 25,000 美元
  → gateway：金額超過門檻，回傳 pending_approval 與 approval_id
主管執行 agw approvals approve <approval_id>（不能是委託 agent 的那個人）
agent 用同樣的參數再送一次
  → gateway：核准有效、參數相符，放行這一次
```

---

## 設計原則

agent 執行有副作用的動作（例如凍結帳戶、送出申報）之前，gateway 會先確認三件事：發起的 agent 與委託人是誰、policy 是否允許這個動作、是否需要人工核准。

這些判斷不交給模型處理，因為 prompt injection 可能讓 agent 執行非預期的指令。gateway 是唯一的執行點：agent 不持有下游系統的憑證，所有 tool 呼叫都必須經過 gateway。

各項設計對應的內控原則：

| 原則 | 做法 |
| ---- | -------------- |
| 最小權限 | 每個 agent 版本一把 key，policy 依 agent、使用者角色、tool 與參數值判斷 |
| 職責分離（maker-checker） | agent 只能發起高風險動作，要由另一位主管核准 |
| 可稽核 | 決策寫入 hash chain，任何一筆被改過都查得出來 |
| 預設拒絕 | token 無效、policy 沒回應、資料庫或稽核寫入失敗時，一律拒絕 |

## 功能

- **LLM 與 MCP 的統一入口**：模型別名、virtual key、rpm 與預算限制、依 key 計費，以及 MCP tool 權限與定義鎖定，這些都由 LiteLLM 提供
- **委託授權**：驗證使用者的 OIDC token，再透過 OAuth token exchange（RFC 8693）換成只能給特定下游使用的 token
- **agent 版本控管**：virtual key 以 `<agent>@<版本>` 命名，只有註冊過的版本能呼叫；改版就要重新審核
- **policy as code**：用 OPA（Rego）寫規則，依參數值決定放行、拒絕或送審；規則和資料由治理團隊用 GitOps 維護
- **maker-checker 審批**：審批是非同步的，核准會綁定呼叫參數、只能用一次、會過期；policy 多了新的送審原因時，要重新核准
- **防竄改稽核**：稽核紀錄以 hash chain 串起來，可以用 API 或 CLI 驗證整條紀錄
- **審批 CLI**：`agw` 用 device flow 登入，可以查看、核准、駁回待審項目
- **LLM guardrail**：檢查 tool 結果中的 prompt injection，並以 spotlighting 標出外部資料的邊界；送給模型前把 PII 換成代號，回應時換回原值，對照表只存在單次請求
- **模型 fallback 與 tpm 限流**：Gemini 失敗時改送 `gpt-6-luna`；每把 agent key 有每分鐘 token 上限

## 運作方式

```mermaid
flowchart LR
    agent(["AI agent"]) -->|"virtual key + 使用者 token"| gw
    sup(["主管<br/>agw CLI"]) -->|"Keycloak token"| gw
    subgraph gw["gateway（單一容器、單一 process、:4000）"]
        litellm["LiteLLM<br/>/v1、/mcp"]
        hook["治理 hook"]
        engine["治理引擎<br/>決策、審批、稽核"]
        api["/governance API"]
        litellm --> hook --> engine
        api --> engine
    end
    engine -->|內部網路| opa["OPA<br/>policies/"]
    engine --> pg[("Postgres")]
    gw -->|"驗章、token exchange"| kc[("Keycloak")]
    gw -->|"放行後（內部網路）"| ops["下游 MCP server"]
```

LLM 請求（`/v1/chat/completions`）經過另一組 hook：送出前檢查注入並假名化 PII，回應時還原代號，見 [LLM guardrail](#llm-guardrail)。

每次 tool 呼叫依序經過這幾步：

1. 驗證使用者 token：檢查簽章、issuer、audience、到期時間，以及是哪個 client 發的。服務帳號的 token 不能當成委託人
2. 從 virtual key 的名稱解析出 agent 與版本
3. 把 agent、使用者、tool 和參數交給 OPA，得到 `allow`、`deny` 或 `require_approval`
4. 需要核准時，用 fingerprint（agent 版本、委託人、tool、參數）找已核准、還沒用過的審批。找到就放行一次，找不到就建立新的待審項目，或沿用既有的
5. 把決策寫進 hash chain

被拒絕或需要等待核准時，agent 收到的 tool result 會是 `isError=True`，內容是 JSON：

```json
{"governance": "pending_approval", "approval_id": "apr_…", "reasons": ["amount_over_threshold"], "expires_at": "…"}
{"governance": "denied", "reasons": ["role_required"]}
```

## 快速開始

需要 Docker（含 Compose）、[uv](https://docs.astral.sh/uv/)、Python 3.12 以上。只有 LLM 流量會用到 Gemini API key；`OPENAI_API_KEY` 選填，只有 fallback 會用到。

```bash
# 1. 建立 .env，填入 LITELLM_MASTER_KEY（sk- 開頭）、POSTGRES_PASSWORD、GOOGLE_API_KEY、KEYCLOAK_ADMIN_PASSWORD
cp .env.example .env

# 2. 安裝套件並啟動服務
uv sync
docker compose up -d --build

# 3. 建立 agent 的 virtual key，存到 var/keys.json
uv run python -m scripts.make_keys

# 4. 跑一次完整的治理示範
uv run python -m scripts.demo_m2

# 5. 跑一次 LLM guardrail 的示範（使用測試 provider probe_llm，不花 LLM 費用）
uv run python -m scripts.demo_m4
```

示範腳本會跑 13 個情境，全部通過時最後一行是 `13/13 通過`：

| 情境 | 預期結果 |
| ---- | -------- |
| 低風險動作、小額凍結 | 放行 |
| 大額凍結 → 主管核准 → 重送 | 放行一次，第三次送出又要重新審批 |
| 委託人自己核准 | `403 self_approval_forbidden` |
| 核准後改了金額 | 要重新審批 |
| 涉及制裁地址、送出 STR | 一律送審 |
| 使用未註冊的 agent 版本 | 拒絕 |
| 核准後 agent 被停用 | 拒絕，核准作廢 |
| 委託人缺少角色、OPA 停止運作 | 拒絕 |
| 駁回後重送 | 產生新的審批，保留歷史紀錄 |
| 直接竄改一筆稽核紀錄 | 驗證時指出是哪一筆 |

過程中腳本會暫時修改 agent 註冊表、重啟 OPA、竄改一筆稽核紀錄，結束前會全部還原。

`demo_m4` 同樣跑 13 個情境，最後一行是 `13/13 通過`：

| # | 情境 | 預期結果 |
| - | ---- | -------- |
| 1 | 使用者訊息含身分證字號 | provider 收到 `<TW_ID_1>`，agent 收到原值 |
| 2 | 模型回傳的 tool call 參數含 `<EMAIL_1>`，原值帶引號 | agent 收到合法的 JSON 與原值 |
| 3 | Ethereum 地址與交易 hash | 不被假名化 |
| 4 | kyt 的 tool 結果含中文覆寫指令 | 內容換成固定文字，寫入稽核 |
| 5 | 同樣內容，沒有 guardrail 設定的 agent | `400 prompt_injection_detected` |
| 6 | tool 結果含零寬字元 | 被移除，稽核記為 `hidden.chars` |
| 7 | tool 結果試圖提前關閉邊界 | 內容被移除，邊界維持完整 |
| 8 | 一般的 kyt 工具輸出 | 沒有任何發現，也沒有稽核紀錄 |
| 9 | 主力模型回 503 | 由 fallback 回應，fallback 收到的也是假名化後的內容 |
| 10 | 超過 tpm 上限 | 第一次 200，之後 429 |
| 11 | OPA 停止運作 | `503 policy_unavailable` |
| 12 | streaming 回應中代號被切成兩段 | 正確還原 |
| 13 | 驗證稽核 | hash chain 完整，資料庫中找不到 PII 原值 |

過程中腳本會暫時停止 OPA，並建立一把用完即刪的 tpm 測試 key。

Keycloak 裡的測試帳號有 `alice`（分析師）、`bob`（主管）、`carol`（兩種角色都有）。這些帳號和 client secret 都是 `keycloak/realm-governance.json` 裡的測試值，只能在本機使用。

## 使用方式

### agent 如何透過 gateway 呼叫工具

agent 用 MCP client 連到 `http://localhost:4000/mcp`，並帶上兩個 header：

| Header | 內容 |
| ------ | ---- |
| `x-litellm-api-key` | `Bearer <agent 版本的 virtual key>`，例如 `kyt-agent@1.5` 的 key |
| `Authorization` | `Bearer <委託人的 Keycloak access token>` |

`Authorization` 只能有一個。如果還帶了其他以 `authorization` 開頭的 header，gateway 會回 `400 ambiguous_authorization_headers`，以免稽核記錄的使用者和下游實際執行的使用者對不上。

收到 `pending_approval` 時，agent 記下 `approval_id`，等主管核准後，用一模一樣的參數再送一次就好，不需要額外的參數。重送前可以用委託人的 token 呼叫 `GET /governance/approvals/{id}/status` 確認是否已核准，避免在待審期間重送。可以參考 [`scripts/agent_sim.py`](scripts/agent_sim.py)；實際接入的例子是 kyt_agent v1.5。

LLM 的請求用 OpenAI 相容格式送到 `http://localhost:4000/v1/chat/completions`，模型名稱用 `litellm/config.yaml` 裡定義的別名。LLM key 只能呼叫 chat completions，請求內容會經過 [LLM guardrail](#llm-guardrail)。

### 用 `agw` CLI 審批

```bash
uv run agw login                                   # 以 Keycloak device flow 登入
uv run agw approvals list                          # 預設列出待審項目；--status approved|rejected|expired|used、--all
uv run agw approvals show <approval_id>            # 詳情，以及同一呼叫的歷史紀錄
uv run agw approvals approve <approval_id> --comment "已確認制裁名單命中"
uv run agw approvals reject <approval_id> --comment "金額有誤"
uv run agw audit verify                            # 驗證整條稽核紀錄
```

- 要有 `compliance_supervisor` 角色，而且不能核准自己委託的項目
- token 存在 `~/.config/ai-gov-gateway/token.json`（檔案 0600、目錄 0700），過期後要重新登入
- 可以用 `AGW_GATEWAY_URL`、`AGW_OIDC_ISSUER`、`AGW_CLIENT_ID`、`AGW_TOKEN_PATH` 覆寫預設設定

### 審批 API

請求需帶 Keycloak access token（`Authorization: Bearer`）。除了狀態查詢，其餘端點都要求 `compliance_supervisor` 角色：

| 方法與路徑 | 用途 |
| ---------- | ---- |
| `GET /governance/approvals?status=pending` | 列出審批 |
| `GET /governance/approvals/{id}` | 查看詳情與歷史 |
| `GET /governance/approvals/{id}/status` | 查詢審批狀態，供 agent 在重送前確認；委託人本人或主管可查，其他人一律 404。只回傳 `id`、`status`、`reasons`、`expires_at`、`decided_at`，不含參數與核准意見 |
| `POST /governance/approvals/{id}/approve` | 核准，body 為 `{"comment": "…"}`，最多 2000 字 |
| `POST /governance/approvals/{id}/reject` | 駁回 |
| `GET /governance/audit/verify` | 驗證稽核紀錄，回傳 `{"records": N, "first_invalid_id": null}` |

<details>
<summary>錯誤代碼</summary>

| 狀態碼 | `detail` | 發生情況 |
| ------ | -------- | -------- |
| 400 | `ambiguous_authorization_headers` | 除了 `Authorization` 之外，還有其他以 `authorization` 開頭的 header |
| 401 | `missing_token` | 沒有帶 token |
| 401 | `invalid_token` | 簽章、issuer、audience、到期時間或發行的 client 不符 |
| 403 | `role_required` | 沒有 `compliance_supervisor`，或沒有這筆審批要求的角色 |
| 403 | `self_approval_forbidden` | 核准者就是委託人 |
| 404 | `approval_not_found` | 找不到這筆審批 |
| 409 | `approval_not_pending` | 審批已經核准、駁回或使用過 |
| 409 | `approval_expired` | 審批已過期 |
| 422 | （驗證錯誤） | `comment` 超過 2000 字，或 `status` 不是有效的審批狀態 |
| 500 | `governance_store_error` | 資料庫發生其他錯誤 |
| 503 | `governance_store_unavailable` | 連不上資料庫 |

</details>

## LLM guardrail

agent 的調查工具多半在本地執行，外部資料（鏈上字串、文件、網頁）以 `role: tool` 訊息出現在下一次 LLM 請求裡，不會經過 MCP。所以 guardrail 放在 LLM 請求的入口與回應：不論 tool 在本地還是經 MCP 執行，結果都會在這裡被檢查。

### 處理順序

送出前（pre-call hook）：

1. 只接受 chat completions（call type 為 `acompletion`），其他呼叫回 `400 call_type_not_governed`
2. 檢查請求欄位：guardrail 只處理訊息內容與 tool 參數，白名單以外的欄位回 `400 unsupported_field`
3. 向 OPA 查詢這個 agent 的設定；agent 已停用時回 `403 agent_suspended`
4. 檢查大小：單則訊息超過 `GOV_GUARDRAIL_MAX_CHARS`、整個請求超過 `GOV_GUARDRAIL_MAX_TOTAL_CHARS` 時拒絕，不截斷
5. tool 結果（`role: tool` 與舊版的 `function`）先正規化：移除零寬、雙向控制等不可見字元，再做 NFKC
6. 以 `guardrails/injection_rules.json` 的規則比對正規化後的 tool 結果，依 agent 設定處理
7. 所有訊息的 PII 換成代號，包括內容、歷史中的 tool call 參數、`name`、`tool_call_id` 與 tool call 的 `id`
8. tool 結果包上邊界：

   ```
   <<tool_output name="get_counterparties">>
   （tool 原本的內容）
   <</tool_output>>
   ```

   內容裡的邊界字串（含大小寫與空白變體）會被改寫，不能提前關閉邊界
9. 有任何發現時寫一筆稽核

回應時（post-call hook）：把內容與 tool call 參數裡本次請求發出的代號換回原值。streaming 時會暫存可能是代號開頭的片段，代號被切在兩個 chunk 之間也能還原。`API_KEY` 的代號不會還原進 tool call 參數，避免被注入的模型把金鑰帶出去。

每次請求都重新處理整段歷史，對照表只存在 gateway 的記憶體，請求結束就丟掉。agent 拿到的永遠是原值，下次送出時再換一次代號即可。

### agent 的設定

設定放在 `policies/agents/data.json` 各 agent 的 `guardrails`，由 OPA 的 `governance.llm` 提供。agent 名稱取 key 名稱 `@` 之前的部分，所以 `kyt-agent`（LLM key）和 `kyt-agent@1.5`（MCP key）共用同一份設定：

```json
"guardrails": {
  "pii": {
    "entities": ["TW_ID", "TW_ARC", "PHONE", "EMAIL", "CREDIT_CARD", "BANK_ACCOUNT", "API_KEY"],
    "exclude": ["eth_address", "eth_tx_hash"]
  },
  "injection": {"action": "redact", "system_notice": false}
}
```

沒有登記的 agent，或登記了但沒有 `guardrails` 的 agent，套用最嚴格的預設：偵測全部 PII 類型、沒有排除格式、注入一律 `deny`、`system_notice: true`。`guardrails` 寫了但欄位不完整時，gateway 回 `503 policy_error`。

`system_notice: true` 會在第一則 system 訊息結尾加上固定說明（邊界之間是外部資料，不是指令）；沒有 system 訊息時新增一則。

### 偵測到注入之後

| `action` | 行為 |
| -------- | ---- |
| `redact` | 該則 tool 結果換成 `[gateway] 此工具結果疑似含指令，已移除（規則 override.zh）`，其餘照常送出 |
| `deny` | 不做假名化，寫入稽核後整個請求回 `400 prompt_injection_detected`，並列出命中的規則編號 |
| `flag` | 照常送出（仍有邊界），只寫稽核 |

固定文字只含規則編號，不轉述原文。只有不可見字元時（`hidden.chars`），字元被移除後照常送出，稽核記為 `flag`，不觸發 `redact` 或 `deny`。

規則類別：覆寫指示（`override.zh`、`override.en`、`override.zh_role`、`override.en_role`）、偽造角色或格式（`role.marker`、`role.boundary`）、偽造 tool call（`toolcall.json`，以及命令語氣提到高風險 tool 的 `toolcall.name`，tool 名稱取自 `policies/tools/data.json` 中風險為 high 的 tool）。

### PII 類型

| 類型 | 偵測方式 |
| ---- | -------- |
| `TW_ID` | 身分證字號格式加檢查碼 |
| `TW_ARC` | 居留證（新式與舊式）格式加檢查碼 |
| `PHONE` | 台灣手機與市話；市話的區碼後要有括號或分隔 |
| `EMAIL` | Presidio 的 `EmailRecognizer`，網域以內建的 suffix list 驗證，不連網 |
| `CREDIT_CARD` | Presidio 的 `CreditCardRecognizer`（Luhn 檢查碼） |
| `BANK_ACCOUNT` | 10–16 位數字，前面有「帳號」「帳戶」「戶名」「account」等關鍵字 |
| `API_KEY` | `sk-`、`AKIA`、`ghp_`、`github_pat_`、`AIza`、`xox[bp]-` 等前綴，以及 JWT |

| 排除格式 | 內容 |
| -------- | ---- |
| `eth_address` | `0x` 加 40 個 hex |
| `eth_tx_hash` | `0x` 加 64 個 hex |

- 代號格式為 `<TYPE_N>`，同一個值在同一次請求中對應同一個代號。原文本來就有這種格式的字串時，編號會跳過，還原時也只換本次請求發出的代號
- 偵測時全形字元先轉半形，緊鄰中文的 email 與卡號也偵測得到
- 某個值在任何一則訊息被偵測到，其他訊息裡照抄的同一個值也會被遮蔽，即使那裡沒有「帳號」之類的關鍵字
- 排除格式由 agent 設定引用，policy 資料不接受任意的正規表示式
- 每次請求最多 1,000 個不同的值、單一值最多 2,048 字元，超過時拒絕

### 錯誤代碼

LiteLLM 會把錯誤包成 OpenAI 格式：HTTP 狀態碼不變，`error.provider_specific_fields` 是 `{"governance": "denied", "reasons": ["<代碼>"]}`（注入時另有 `rules`，欄位錯誤時另有 `field`），`error.message` 是同一份內容的 JSON 字串。回應只帶代碼，不含原文。

| 狀態碼 | 代碼 | 發生情況 |
| ------ | ---- | -------- |
| 400 | `prompt_injection_detected` | tool 結果命中注入規則，且 agent 設定為 `deny` |
| 400 | `content_too_large` | 單則訊息或整個請求超過掃描上限（NFKC 展開後也會再檢查） |
| 400 | `unsupported_field` | 請求帶了 guardrail 不處理的欄位，`field` 為欄位名稱 |
| 400 | `invalid_messages` | `messages` 不是物件陣列 |
| 400 | `too_many_pii_values` | 單次請求的不同 PII 值超過 1,000 個 |
| 400 | `pii_value_too_long` | 單一 PII 值超過 2,048 字元 |
| 400 | `call_type_not_governed` | 非 chat completions 的呼叫進入 hook（例如 master key 呼叫 embeddings） |
| 403 | `agent_suspended` | agent 已停用 |
| 502 | `guardrail_output_error` | 回應的代號還原失敗，不回傳半還原的結果 |
| 503 | `policy_unavailable` | OPA 沒有回應或逾時 |
| 503 | `policy_error` | OPA 回傳的設定不完整或名稱不支援 |
| 503 | `guardrail_unavailable` | 規則比對或 PII 偵測發生例外 |
| 503 | `audit_write_failed` | 有發現但稽核寫入失敗 |

### 稽核事件

只有偵測到 PII、注入或不可見字元時，才寫一筆 `event_type = llm_guardrail` 的紀錄，接在同一條 hash chain 上：

| 欄位 | 內容 |
| ---- | ---- |
| `agent_id`、`key_alias` | 呼叫的 agent 與 key |
| `decision` | 有注入時為 agent 的 `action`；沒有注入但有不可見字元時為 `flag`；只有 PII 時為 `pass` |
| `reasons` | 命中的規則編號 |
| `policy_version` | `policies/meta/data.json` 的版本 |
| `details.model`、`details.call_id` | 模型別名與 LiteLLM 的 call id，可對照 LiteLLM 的用量紀錄 |
| `details.pii` | 各類型的數量，例如 `{"TW_ID": 1}` |
| `details.injection` | 每筆發現的規則編號、tool 名稱、`tool_call_id`、內容的 sha256 與處理方式 |
| `details.rules_version` | 注入規則檔的版本 |

不記錄 PII 原值與 tool 原文。

### fallback 與 tpm

- `kyt-main`（Gemini）重試一次仍失敗時，改送 `kyt-main-fallback`（`openai/gpt-6-luna`）。fallback 收到的是假名化後的內容
- LiteLLM 不能依狀態碼限制 fallback：主力回 400、401、403、404 也會改送 fallback。Gemini 的 key 設錯時 client 仍拿到正常回應，問題只會出現在 gateway 日誌
- `.env` 沒有 `OPENAI_API_KEY` 時 fallback 會失敗，client 收到主力原本的錯誤
- fallback 不檢查 key 的 `models` 與預算，`kyt-agent` 的 key 只列 `kyt-main` 也能用到 `gpt-6-luna`
- 一次呼叫最多送出主力 2 次、fallback 2 次；kyt 本身也會重試，故障時呼叫量會疊加
- `kyt-agent` 的 key 設 `tpm_limit: 300000`。用量在回應後才累計：超過上限的那個請求仍會放行，之後的請求回 429
- 真實模型的驗證：fallback 的路由已確認（主力回 503 後確實改送 OpenAI），`gpt-6-luna` 的實際回應待驗證，因為 OpenAI 帳戶額度不足，見 [findings 的「真實 LLM 驗證」](docs/spikes/m4-findings.md)

## 撰寫 policy

規則和資料放在 `policies/`，由治理團隊維護，經過 PR 審查才會生效。OPA 以 `--watch` 模式載入，改完不必重啟。

```
policies/
├── governance.rego          # 規則：先依序檢查拒絕條件，再收集送審原因
├── governance_llm.rego      # LLM guardrail 的設定（governance.llm）與未登記 agent 的預設
├── governance_test.rego、governance_llm_test.rego  # Rego 測試，包含資料完整性檢查
├── agents/data.json         # agent 註冊表：負責人、狀態、能承擔的風險等級、核准的版本與 key、guardrail 設定
├── tools/data.json          # 各 tool 的風險等級與門檻
├── sanctions/data.json      # 制裁地址清單
└── meta/data.json           # policy_version，會記進稽核紀錄
```

目前的規則：

| 情況 | 結果 |
| ---- | ---- |
| agent 版本沒註冊、agent 已停用、tool 沒登記、風險超出 agent 的上限、委託人缺少角色 | 拒絕 |
| 凍結金額達到門檻、交易對手在制裁名單上、參數缺漏或格式錯誤、送出 STR、新加入的高風險 tool 還沒寫規則 | 送審 |
| 其他低風險動作、門檻內的凍結 | 放行 |

常見的操作：

- 上線新的 agent 版本：在 `agents/data.json` 的 `versions` 加上 `key_alias`，再建立同名的 virtual key（`scripts/make_keys.py` 會建立缺少的 key，並以 `/key/update` 同步既有 key 的權限）。目前註冊的是 `kyt-agent@1.4` 與 `kyt-agent@1.5`；`kyt-agent@1.5` 的 key 只開放 `account_ops` 的 `add_to_watchlist` 與 `file_str`
- 修改規則：記得更新 `meta/data.json` 的 `policy_version`，送 PR 前先跑下面三個指令
- 修改注入規則（`guardrails/injection_rules.json`，不在 `policies/` 裡）：更新檔案中的 `rules_version`，並跑 `uv run pytest tests/test_guardrail_injection.py`，其中包含以 kyt 實際工具輸出做的誤判測試。規則檔在 gateway 啟動時載入，改完要重建映像

```bash
docker run --rm -v "$PWD/policies:/policies:ro" openpolicyagent/opa:1.21.1 test /policies
docker run --rm -v "$PWD/policies:/policies:ro" openpolicyagent/opa:1.21.1 fmt --list /policies
docker run --rm -v "$PWD/policies:/policies:ro" openpolicyagent/opa:1.21.1 check --strict /policies
```

寫規則時要注意一個陷阱：在 Rego 裡引用不存在的欄位，結果是 undefined，對它做 `not` 或比較也一樣是 undefined，而不是 true。如果直接寫 `not input.arguments.amount_usd >= ...` 這類條件，參數缺漏時規則就會靜悄悄地放行。所以檢查條件要先寫成正面的 helper 規則，再對 helper 做 `not`。

## 服務與設定

| 服務 | 位置 | 用途 |
| ---- | ---- | ---- |
| `gateway` | `:4000` | LiteLLM v1.105.1 加上治理套件 |
| `keycloak` | `:8080` | IdP，啟動時匯入 `keycloak/realm-governance.json` |
| `postgres` | `127.0.0.1:55433` | `litellm` 資料庫存 key 與用量，`governance` 資料庫存審批與稽核 |
| `opa` | 內部網路 | OPA 1.21.1，不對外開放 |
| `account_ops` | 內部網路 | 示範用的下游 MCP server，提供 `add_to_watchlist`、`freeze_account`、`file_str` |
| `probe_mcp` | `127.0.0.1:8100` | 除錯用的上游 MCP server，會回傳收到的 header 與 token claims |
| `probe_llm` | `127.0.0.1:8300` | OpenAI 相容的測試 provider：記錄收到的請求，依 system 訊息中的 `probe:` 指令回傳文字、tool call、503 或指定的 token 用量，供 `demo_m4` 與整合測試使用 |

`opa` 和 `account_ops` 只接在 `internal: true` 的 `governed` 網路上，只有 gateway 連得到。

<details>
<summary>治理相關的環境變數（<code>GOV_*</code>）</summary>

gateway 從容器的環境變數讀取這些設定，值寫在 `docker-compose.yml`：

| 環境變數 | 預設值 | 用途 |
| -------- | ------ | ---- |
| `GOV_DATABASE_URL` | （必填） | 治理資料庫 |
| `GOV_OPA_URL` | `http://opa:8181` | OPA 的 REST API |
| `GOV_OPA_TIMEOUT_SECONDS` | `2.0` | 查詢 OPA 的逾時秒數，逾時視為 `policy_unavailable` |
| `GOV_OIDC_ISSUER` | `http://localhost:8080/realms/governance` | 使用者 token 的 `iss` |
| `GOV_OIDC_JWKS_URL` | `http://keycloak:8080/realms/governance/protocol/openid-connect/certs` | 驗章用的 JWKS |
| `GOV_OIDC_AUDIENCE` | `ai-gateway` | 使用者 token 必須包含的 `aud` |
| `GOV_OIDC_ALLOWED_CLIENTS` | `["kyt-cli", "agw-cli"]` | 可以發行使用者 token 的 client，格式為 JSON 陣列，不可為空 |
| `GOV_APPROVAL_TTL_HOURS` | `24` | 審批有效期限（小時） |
| `GOV_APPROVER_ROLE` | `compliance_supervisor` | 核准所需的角色 |
| `GOV_GUARDRAIL_RULES_PATH` | `/opt/ai-gov-gateway/guardrails/injection_rules.json` | 注入規則檔（映像內的路徑） |
| `GOV_GUARDRAIL_MAX_CHARS` | `200000` | 單則訊息的掃描上限（字元），超過時回 `content_too_large` |
| `GOV_GUARDRAIL_MAX_TOTAL_CHARS` | `1000000` | 整個請求的掃描上限（字元） |

</details>

## 專案結構

```
ai-gov-gateway/
├── src/ai_gov_gateway/
│   ├── plugin.py            # LiteLLM 載入的進入點，負責掛上 /governance 路由與治理 hook
│   ├── hook_adapter.py      # 把 LiteLLM hook 的資料轉成治理引擎的輸入
│   ├── engine/              # 治理引擎（不依賴 LiteLLM）：決策、審批、稽核、token 驗證，以及 OPA 與 Postgres 的介接
│   │   └── guardrails/      # LLM guardrail：注入規則與 spotlighting、PII 偵測與假名化、請求欄位白名單
│   ├── api/approvals.py     # /governance 路由
│   └── cli.py、cli_auth.py  # agw CLI
├── policies/                # OPA 規則與資料
├── guardrails/              # 注入規則 injection_rules.json、Presidio 相依的版本鎖定
├── litellm/config.yaml      # 模型別名、fallback、MCP server、guardrail 設定
├── keycloak/                # 本機測試用的 realm
├── account_ops/、probe_mcp/  # 示範與除錯用的 MCP server
├── probe_llm/               # 測試用的 OpenAI 相容 provider
├── scripts/                 # 示範、建立 key 與驗證用的腳本
├── tests/                   # 單元測試與整合測試
└── docs/                    # 設計、實作計畫與技術驗證紀錄
```

治理引擎只透過 `PolicyClient`（OPA）和 `GovernanceStore`（Postgres）兩個介面取得外部資料，不用啟動 LiteLLM 也能完整測試。

## 開發

```bash
uv run pytest                          # 單元測試，不需要 Docker
uv run pytest -m integration           # 整合測試，需要先啟動 docker compose
uv run ruff format . && uv run ruff check .
uv run mypy src
```

| 測試 | 數量 | 涵蓋範圍 |
| ---- | ---- | -------- |
| 單元測試 | 384 | 引擎、審批、token 驗證、稽核、API、CLI、hook 轉接；LLM guardrail 的注入規則（含 kyt 工具輸出的誤判語料與 20 萬字元的計時測試）、PII 偵測與假名化、請求欄位、還原與 streaming |
| 整合測試 | 31 | Postgres store 12（含並行寫入與數值精確度）、gateway 與 LiteLLM 的介面契約 7（含狀態查詢 API）、LLM guardrail 的 hook 契約 12（改寫與還原、streaming、路由限制、fallback、tpm） |
| Rego 測試 | 42 | 每條規則、欄位缺漏或資料錯誤時的預設拒絕、資料完整性、LLM guardrail 的預設與設定驗證 |
| 端到端示範 | 13 + 13 個情境 | `scripts/demo_m2.py`、`scripts/demo_m4.py` |

整合測試會用 `.env` 裡的 `GOV_TEST_DATABASE_URL` 連到 `governance_test` 資料庫。測試會刪除資料表，所以資料庫名稱不是以 `_test` 結尾時會直接跳過。

LLM guardrail 的契約測試需要 `probe_llm` 與 `scripts.make_keys` 建立的 `kyt-agent@llm-demo`、`guard-strict` key。

升級 LiteLLM 前一定要先跑整合測試與 `demo_m4`。治理 hook 依賴 LiteLLM 的 guardrail 介面、欄位名稱與 hook 的呼叫順序，這些一有變動，介面契約測試就會失敗。

## 已知限制

<details>
<summary>展開</summary>

- LiteLLM 管理者可以在 key 或 team 的 metadata 設定 `disable_global_guardrails` 或 `opted_out_global_guardrails`，讓該 key 的呼叫跳過治理。所以要限制誰能管理 key 和 team，並定期檢查這些 metadata
- 只記錄決策，不記錄下游的執行結果。核准在放行時就標記為已使用，下游如果執行失敗，要重新申請
- tool 清單不經過治理。未註冊版本的 key 還是看得到 tool，只是呼叫時會被拒絕
- OPA 和 `account_ops` 本身不驗證呼叫者，安全性靠內部網路隔離
- `/mcp-rest/tools/call` 被擋下時會回 HTTP 500，治理訊息包在 `detail.message` 裡
- WebSocket 路由不在 `Authorization` header 的檢查範圍內
- 取不到 JWKS 時會回報 `invalid_user_token`；policy 資料有誤時，原因代碼沿用 `risk_exceeds_agent_limit` 或 `agent_suspended`
- LiteLLM 會把每次治理拒絕以 ERROR 等級連同 traceback 寫進日誌
- device flow 只用 mock 測試過，自動化腳本改用 password grant 搭配測試帳號

LLM guardrail：

- LLM 請求從 M4 起依賴 OPA。OPA 停擺時所有 LLM 流量都會失敗（`503 policy_unavailable`），可用性要求比只影響高風險 tool 時更高
- 規則比對換個說法就能繞過（例如 Ignore everything above、同形字）。它是多層防護的一層，搭配 spotlighting、高風險 tool 的主管核准與 agent 自身的防護，不保證擋下所有注入
- tool 結果在送進 LLM 時才檢查，agent 本地已經讀到原文；LLM 不經 gateway 的 agent 不受保護
- 只有 tool 結果會正規化。其他訊息被零寬字元拆開的 email 不會被遮蔽
- 只偵測固定格式的 PII，人名、地址不會被假名化；圖片等非文字內容不掃描
- `user` 欄位，以及 `tools` 的描述、`response_format` 的 schema、`stop`、tool 名稱不遮蔽，這些視為開發者定義的內容
- 欄位白名單也會拒絕 `presence_penalty`、`logprobs`、`reasoning_effort`、`cache_control` 等正常參數，需要時再加入 `engine/guardrails/fields.py`
- 受治理的 key 只能呼叫 chat completions：`/v1/models`、Azure 風格的路由、provider pass-through 都會被擋
- 含 PII 的 tool call id 遮蔽後含有 `<` `>`，部分 provider 會以 400 拒絕
- 不要啟用 `store_prompts_in_spend_logs`、記錄訊息內容的 callback 或回應快取：LiteLLM 的 `proxy_server_request` 仍是原始內容；快取鍵是假名化後的 prompt，不同使用者的不同原值都可能變成 `<TW_ID_1>`，彼此命中對方的快取
- 稽核的 `content_sha256` 對低熵的內容（例如短字串）可以用字典查回原文
- streaming 最後一個 chunk 只帶 usage 時，暫存中的文字可能遺失
- tpm 或 rpm 觸發時，LiteLLM 會以新的 call id 重跑 pre-call hook，同一個請求會寫兩筆稽核

</details>

## 開發進度

| 階段 | 內容 | 狀態 |
| ---- | ---- | ---- |
| M1 | 技術驗證：LiteLLM 的 LLM 與 MCP gateway、Keycloak、token exchange | 完成 |
| M2 | 治理服務：policy、agent 版本、maker-checker 審批、hash chain 稽核 | 完成 |
| M3 | kyt_agent v1.5 經 gateway 執行列入監控名單與送出 STR，等待核准時以狀態查詢 API 確認後重送 | 完成 |
| M4 | LLM guardrail（注入防護與 spotlighting、PII 假名化）、`gpt-6-luna` fallback、tpm 限流 | 完成 |

## 文件

- [M4 LLM guardrail 設計](docs/specs/2026-10-23-m4-guardrails-design.md)：guardrail 的處理順序、PII 假名化、OPA 設定、fallback 與 tpm；實作時的調整記錄在 §13
- [M4 技術驗證結果](docs/spikes/m4-findings.md)：Presidio 的打包、LiteLLM hook 的行為（改寫、還原、streaming、fallback、tpm），以及真實 LLM 的驗證
- M3 的設計在 kyt_agent repo 的 `docs/specs/2026-10-21-kyt-agent-v1.5-design.md`（§8 為 gateway 的改動，§11 為實作後修訂）
- [M2 治理服務設計](docs/specs/2026-10-20-m2-governance-design.md)：架構、決策流程、policy、審批與稽核；實作時的調整記錄在 §11
- [M1 技術驗證結果](docs/spikes/m1-findings.md)：LiteLLM 開源版能做到哪些事，以及為什麼治理要放在 hook
- [實作計畫](docs/plans/)
