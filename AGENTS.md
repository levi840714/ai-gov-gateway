# AGENTS.md

AI Governance Gateway（`ai-gov-gateway`）：統一治理 agent 的 LLM 流量與 tool 呼叫，提供 agent 身份、委託授權、policy 判斷、人工審批與稽核。
以 LiteLLM 作為 LLM 與 MCP 的 data plane，治理邏輯以 Python 實作。
設計文件在 `docs/specs/`，實作計畫在 `docs/plans/`，技術驗證結果在 `docs/spikes/`。

## 開發階段

| 階段 | 內容 | 狀態 |
| ---- | ---- | ---- |
| M1 | 技術驗證：LiteLLM（LLM + MCP gateway）、Keycloak、OBO，決定 M2 架構 | 完成 |
| M2 | 治理服務：以 LiteLLM hook 為執行點，OPA policy、maker-checker 審批、稽核 log、高風險 tool | 完成 |
| M3 | kyt_agent 接入：以 `kyt-agent@<版本>` key 呼叫 account_ops 的高風險 tool，pending 時中斷並以相同參數重送 | 未開始 |

## 目錄結構

```
ai-gov-gateway/
├── Dockerfile               # gateway 映像：LiteLLM v1.105.1 + src/ai_gov_gateway（以 PYTHONPATH 載入）
├── docker-compose.yml       # gateway、postgres、keycloak、opa、account_ops、probe_mcp（image 固定版本）
├── src/ai_gov_gateway/
│   ├── engine/              # 治理引擎：models、fingerprint、tokens、policy、store、postgres、audit、engine、approvals
│   ├── api/                 # /governance 路由（審批、稽核驗證）
│   ├── plugin.py            # LiteLLM guardrail；載入時掛上 /governance 路由（只在 gateway 映像內 import）
│   ├── hook_adapter.py      # LiteLLM hook 的 data → ToolCall
│   ├── runtime.py           # 由 Settings 組出 Services（lazy 單例）
│   ├── config.py            # GOV_ 前綴的設定
│   └── cli.py、cli_auth.py  # agw 指令與 device flow
├── policies/                # governance.rego、Rego 測試、agents/tools/sanctions/meta 的 data.json
├── account_ops/             # 模擬下游 MCP server（三個 tool）
├── litellm/                 # config.yaml（模型別名、MCP server、OBO、pinned_tools、guardrail）
├── keycloak/                # realm-governance.json（測試帳號與 client）
├── probe_mcp/               # 測試用上游 MCP server
├── scripts/                 # M1 驗證腳本、agent_sim、demo_m2
├── tests/                   # 單元測試；integration/ 需要 docker compose 服務
├── docs/                    # specs/、plans/、spikes/
└── var/                     # 執行產物，含 keys.json（不進版控）
```

LiteLLM 固定在 v1.105.1：tool 定義鎖定從此版起才有；`plugin.py` 依賴 `CustomGuardrail` 介面、`litellm.proxy.proxy_server.app` 與 hook 的 `mcp_*` 欄位。
**升級 LiteLLM 前必須跑 `uv run pytest -m integration`**（含 gateway 契約測試），結果須為 14 passed 而非 skipped。

`engine/` 不 import LiteLLM、FastAPI 與 `config`；依賴方向為 `plugin` / `api` → `runtime` → `engine` / `config`，`runtime` 負責把設定注入引擎。

## 工具鏈

- **Python**：3.12 以上
- **套件管理**：`uv`（`uv add`、`uv run`），不直接使用 `pip install`
- **Lint / Format**：`ruff`，line-length 100
- **型別檢查**：`mypy src`，公開函式必須有型別標註
- **測試**：`pytest`；需要 Docker 服務的測試標記 `@pytest.mark.integration`，預設不執行
- **設定**：`pydantic-settings` 讀取 `.env`，程式中不得寫死任何 key
- **Policy**：Rego v1（OPA 1.21.1）；修改 `policies/` 要更新 `meta/data.json` 的 `policy_version` 並通過 `opa test`
- **服務**：`docker compose` 啟動 gateway、Postgres、Keycloak、OPA、account_ops、probe_mcp；image 一律固定版本

```bash
uv sync
docker compose up -d --build
uv run pytest
uv run pytest -m integration
uv run ruff format . && uv run ruff check . && uv run mypy src
docker run --rm -v "$PWD/policies:/policies:ro" openpolicyagent/opa:1.21.1 test /policies
```

## 程式碼規範

- Clean code：單一職責、命名清楚、函式短小、避免過度抽象
- 資料模型使用 `TypedDict` / `pydantic.BaseModel`
- 不留死碼、未使用的 import、註解掉的程式碼
- 只寫必要的註解，說明「為什麼」而非重述程式碼，每段不超過兩行

## 安全

- `.env` 不進版控，只提交 `.env.example`
- Keycloak realm 設定只使用測試帳號與測試密碼，不放真實憑證
- 不在 log 或輸出中印出 API key、token 原文；驗證 token 內容時只輸出 claims
- 含秘密的欄位（`ToolCall.user_token`、`Settings.database_url`）設 `repr=False`；CLI 與 API 的錯誤輸出只帶錯誤代碼或例外類別名稱
- 治理一律 fail-closed：新增程式路徑時，無法判斷或無法寫入稽核都必須拒絕
