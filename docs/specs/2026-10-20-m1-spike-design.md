# M1 技術驗證設計

## 1. 背景

AI Governance Gateway 的目標是統一治理 agent 的 LLM 流量與 tool 呼叫。評估後決定以 LiteLLM 開源版作為 data plane：

- LLM gateway：多 provider、模型別名、virtual key、預算與 rate limit
- MCP gateway：MCP server 註冊、tool 定義鎖定、依 key/team 的 tool 權限、對上游的 OAuth On-Behalf-Of（RFC 8693）

LiteLLM 做不到、需要自建的部分（M2）：以參數值判斷的 policy（OPA）、maker-checker 審批、同時記錄 agent 與使用者身份、防竄改稽核 log。

文件未說明清楚的部分有兩項：OBO 在開源版能否運作（LiteLLM 的 JWT/OIDC 驗證需要企業版授權），以及 governance service 該如何接入 LiteLLM。M1 以實際環境驗證這些前提，結果用來定稿 M2 架構。

**M1 的產出是驗證報告，不是正式功能。**

## 2. 已確定的整體決策

| 項目 | 決策 |
| ---- | ---- |
| 治理範圍 | 只治理有副作用的高風險動作（`add_to_watchlist`、`freeze_account`、`file_str`），kyt_agent 既有唯讀工具不變 |
| 呼叫時機 | agent 在調查中自主呼叫，由 gateway 判斷放行、拒絕或等待核准 |
| Policy 管理 | 中央治理團隊維護，以 GitOps 方式審查變更 |
| 身份 | Keycloak 驗證使用者與 agent；委託 token 的簽發方式依 V4、V5 結果決定 |
| Policy engine | OPA（bundle 模式） |
| 審批 | 非同步：gateway 回傳 pending 與 approval_id，agent 以 interrupt 中斷，核准後 `kyt resume` 接續；核准者不得為委託人 |
| 第一個使用者 | kyt_agent；v2 以 aml_reg_rag 驗證通用性 |

## 3. 環境

`docker compose` 啟動下列服務，image 固定版本：

| 服務 | 用途 |
| ---- | ---- |
| `litellm` | LiteLLM Proxy，LLM 與 MCP gateway |
| `postgres` | LiteLLM 儲存 virtual key 與用量 |
| `keycloak` | Keycloak 26 dev mode，啟動時匯入 `keycloak/realm-governance.json` |
| `probe_mcp` | 自寫的上游 MCP server（Python FastMCP），提供 `whoami` tool，回傳收到的 header 名稱與 Bearer token 的 claims |

Keycloak realm `governance` 內容（全部為測試帳號）：

- 使用者 `alice`（角色 `analyst`）、`bob`（角色 `compliance_supervisor`）
- client `kyt-agent`（confidential，可做 token exchange）
- client `probe-mcp`（作為 token exchange 的 audience）
- client `kyt-cli`（public，供 device flow 登入）

`probe_mcp` 不驗證簽章，只解碼 claims 以觀察內容；正式的驗證屬於 M2。輸出不包含 token 原文。

## 4. 驗證項目

| # | 驗證內容 | 通過標準 |
| - | -------- | -------- |
| V1 | kyt_agent 的 LLM 流量改走 LiteLLM | `kyt eval` 以快照重播少量案例（3 筆），決策與風險等級和直連結果一致；audit log 的 token 數有記錄 |
| V2 | 模型別名下的成本估算 | 確認 `estimate_cost` 在別名下的行為，提出修正方式並實作 |
| V3 | 每個 agent 一把 virtual key，含預算與 rpm 限制 | 超過 rpm 時請求被拒絕；用量可依 key 查詢 |
| V4 | 開源版能否執行 OBO（`oauth2_token_exchange`） | client 帶 alice 的 Keycloak token 呼叫 `whoami`，`probe_mcp` 收到 audience 為 `probe-mcp` 的交換後 token；記錄是否需要企業版授權 |
| V5 | Keycloak token exchange 是否保留 agent 身份 | 檢查交換後 token 的 `sub`、`azp`、`aud`、`act` 等 claims |
| V6 | 上游能取得哪些身份資訊 | 記錄 `probe_mcp` 收到的 header（virtual key 別名、team、end-user 等） |
| V7 | governance service 的接入方式 | 比較「作為上游 MCP server」與「LiteLLM Python hook」：hook 能否攔截 MCP tool call 並回傳自訂結果 |
| V8 | tool 定義鎖定、依 key 的 tool 權限 | 實際設定並測試，記錄是否為企業版功能 |

每一項都記錄：使用的設定、執行的指令、實際輸出（去除 token 與 key 原文）、結論（通過 / 不通過 / 部分通過）。

## 5. kyt_agent 的改動

在 kyt_agent repo 另外 commit，維持向下相容：

- 新增選配設定 `LLM_BASE_URL`、`LLM_API_KEY`；兩者皆有值時，以 OpenAI 相容端點建立模型（`init_chat_model` 的 `base_url`、`api_key`），否則行為不變
- V2 的修正依驗證結果決定，同樣不得影響未設定 proxy 時的行為
- 新增對應的單元測試，既有測試全部通過

## 6. 不在 M1 範圍

OPA、審批、稽核 log、高風險 tool、Python 套件的正式模組。M1 只有 `probe_mcp` 一個程式，以及驗證用的腳本。

## 7. 產出

- 本設計文件
- `docs/spikes/m1-findings.md`：V1–V8 的結果與證據，結尾提出 M2 架構建議（委託 token 由誰簽發、governance service 的接入方式、需要自建的範圍）
- `docker-compose.yml`、`litellm/config.yaml`、`keycloak/realm-governance.json`、`probe_mcp/`、`.env.example`、`README.md`

## 8. 風險

| 風險 | 處理方式 |
| ---- | -------- |
| OBO 需要企業版授權 | 記錄為結論；M2 改由 governance service 自行驗證 Keycloak token 並執行 token exchange |
| Keycloak 不支援 `act` claim | M2 由 governance service 簽發委託 token，或在稽核 log 另外記錄 agent 身份 |
| LiteLLM 改用別名後，kyt_agent 的 token 用量欄位缺失 | 記錄差異，在 kyt_agent 端補上對應 |
| 真實 LLM 呼叫成本 | V1 只跑 3 筆快照重播，V3 以極小的 max_tokens 測試 rpm |
