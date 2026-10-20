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

需要 Docker（含 Compose）、[uv](https://docs.astral.sh/uv/)、Python 3.12 以上。只有 LLM 流量會用到 Gemini API key。

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

Keycloak 裡的測試帳號有 `alice`（分析師）、`bob`（主管）、`carol`（兩種角色都有）。這些帳號和 client secret 都是 `keycloak/realm-governance.json` 裡的測試值，只能在本機使用。

## 使用方式

### agent 如何透過 gateway 呼叫工具

agent 用 MCP client 連到 `http://localhost:4000/mcp`，並帶上兩個 header：

| Header | 內容 |
| ------ | ---- |
| `x-litellm-api-key` | `Bearer <agent 版本的 virtual key>`，例如 `kyt-agent@1.4` 的 key |
| `Authorization` | `Bearer <委託人的 Keycloak access token>` |

`Authorization` 只能有一個。如果還帶了其他以 `authorization` 開頭的 header，gateway 會回 `400 ambiguous_authorization_headers`，以免稽核記錄的使用者和下游實際執行的使用者對不上。

收到 `pending_approval` 時，agent 記下 `approval_id`，等主管核准後，用一模一樣的參數再送一次就好，不需要額外的參數或 API。可以參考 [`scripts/agent_sim.py`](scripts/agent_sim.py)。

LLM 的請求用 OpenAI 相容格式送到 `http://localhost:4000/v1`，模型名稱用 `litellm/config.yaml` 裡定義的別名。

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

請求需帶 Keycloak access token（`Authorization: Bearer`）：

| 方法與路徑 | 用途 |
| ---------- | ---- |
| `GET /governance/approvals?status=pending` | 列出審批 |
| `GET /governance/approvals/{id}` | 查看詳情與歷史 |
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

## 撰寫 policy

規則和資料放在 `policies/`，由治理團隊維護，經過 PR 審查才會生效。OPA 以 `--watch` 模式載入，改完不必重啟。

```
policies/
├── governance.rego          # 規則：先依序檢查拒絕條件，再收集送審原因
├── governance_test.rego     # Rego 測試，包含資料完整性檢查
├── agents/data.json         # agent 註冊表：負責人、狀態、能承擔的風險等級、核准的版本與 key
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

- 上線新的 agent 版本：在 `agents/data.json` 的 `versions` 加上 `key_alias`，再建立同名的 virtual key
- 修改規則：記得更新 `meta/data.json` 的 `policy_version`，送 PR 前先跑下面三個指令

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

</details>

## 專案結構

```
ai-gov-gateway/
├── src/ai_gov_gateway/
│   ├── plugin.py            # LiteLLM 載入的進入點，負責掛上 /governance 路由與治理 hook
│   ├── hook_adapter.py      # 把 LiteLLM hook 的資料轉成治理引擎的輸入
│   ├── engine/              # 治理引擎（不依賴 LiteLLM）：決策、審批、稽核、token 驗證，以及 OPA 與 Postgres 的介接
│   ├── api/approvals.py     # /governance 路由
│   └── cli.py、cli_auth.py  # agw CLI
├── policies/                # OPA 規則與資料
├── litellm/config.yaml      # 模型別名、MCP server、guardrail 設定
├── keycloak/                # 本機測試用的 realm
├── account_ops/、probe_mcp/  # 示範與除錯用的 MCP server
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
| 單元測試 | 165 | 引擎、審批、token 驗證、稽核、API、CLI、hook 轉接 |
| 整合測試 | 16 | Postgres store（含並行寫入與數值精確度），以及 gateway 與 LiteLLM 的介面契約 |
| Rego 測試 | 33 | 每條規則、欄位缺漏或資料錯誤時的預設拒絕、資料完整性 |
| 端到端示範 | 13 個情境 | `scripts/demo_m2.py` |

整合測試會用 `.env` 裡的 `GOV_TEST_DATABASE_URL` 連到 `governance_test` 資料庫。測試會刪除資料表，所以資料庫名稱不是以 `_test` 結尾時會直接跳過。

升級 LiteLLM 前一定要先跑整合測試。治理 hook 依賴 LiteLLM 的 guardrail 介面和欄位名稱，這些一有變動，介面契約測試就會失敗。

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

</details>

## 開發進度

| 階段 | 內容 | 狀態 |
| ---- | ---- | ---- |
| M1 | 技術驗證：LiteLLM 的 LLM 與 MCP gateway、Keycloak、token exchange | 完成 |
| M2 | 治理服務：policy、agent 版本、maker-checker 審批、hash chain 稽核 | 完成 |
| M3 | 讓鏈上風險調查 agent（kyt_agent）接上高風險 tool | 規劃中 |

## 文件

- [M2 治理服務設計](docs/specs/2026-10-20-m2-governance-design.md)：架構、決策流程、policy、審批與稽核；實作時的調整記錄在 §11
- [M1 技術驗證結果](docs/spikes/m1-findings.md)：LiteLLM 開源版能做到哪些事，以及為什麼治理要放在 hook
- [實作計畫](docs/plans/)
