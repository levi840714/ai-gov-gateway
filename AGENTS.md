# AGENTS.md

AI Governance Gateway（`ai-gov-gateway`）：統一治理 agent 的 LLM 流量與 tool 呼叫，提供 agent 身份、委託授權、policy 判斷、人工審批與稽核。
以 LiteLLM 作為 LLM 與 MCP 的 data plane，治理邏輯以 Python 實作。
設計文件在 `docs/specs/`，實作計畫在 `docs/plans/`，技術驗證結果在 `docs/spikes/`。

## 開發階段

| 階段 | 內容 | 狀態 |
| ---- | ---- | ---- |
| M1 | 技術驗證：LiteLLM（LLM + MCP gateway）、Keycloak、OBO，決定 M2 架構 | 完成 |
| M2 | 治理服務：以 LiteLLM hook 為執行點，OPA policy、maker-checker 審批、稽核 log、高風險 tool | 完成 |
| M3 | kyt_agent 接入：以 `kyt-agent@1.5` key 呼叫 account_ops 的 `add_to_watchlist`、`file_str`，pending 時中斷，經狀態查詢 API 確認核准後以相同參數重送 | 完成 |
| M4 | LLM guardrail：tool 結果的注入防護與 spotlighting、PII 假名化（單次請求內可逆）、依 agent 的 OPA 設定；`gpt-6-luna` fallback 與 tpm 限流 | 完成 |

## 目錄結構

```
ai-gov-gateway/
├── Dockerfile               # gateway 映像：LiteLLM v1.105.1 + presidio-analyzer + src/ai_gov_gateway（以 PYTHONPATH 載入）
├── docker-compose.yml       # gateway、postgres、keycloak、opa、account_ops、probe_mcp、probe_llm（image 固定版本）
├── docker-compose.fallback.yml、docker-compose.spike.yml  # 驗證時疊加：強制 fallback、M4 hook 驗證
├── src/ai_gov_gateway/
│   ├── engine/              # 治理引擎：models、fingerprint、tokens、policy、store、postgres、audit、engine、approvals
│   │   └── guardrails/      # LLM guardrail：models、injection（規則、正規化、spotlighting）、pii（偵測、假名化、還原）、fields（請求欄位白名單）、service（LlmGuardrail）
│   ├── api/                 # /governance 路由（審批、狀態查詢、稽核驗證）
│   ├── plugin.py            # LiteLLM guardrail（GovernanceGuardrail、LlmGuardrailHook）；載入時掛上 /governance 路由與 Authorization header 檢查（只在 gateway 映像內 import）
│   ├── hook_adapter.py      # LiteLLM hook 的 data → ToolCall；LLM 回應與 streaming 的還原、pre-call 到 post-call 之間的對照表
│   ├── runtime.py           # 由 Settings 組出 Services（lazy 單例）
│   ├── config.py            # GOV_ 前綴的設定
│   └── cli.py、cli_auth.py  # agw 指令與 device flow
├── policies/                # governance.rego、governance_llm.rego、Rego 測試、agents/tools/sanctions/meta 的 data.json
├── guardrails/              # injection_rules.json（注入規則與 rules_version）、presidio-constraints.txt
├── account_ops/             # 模擬下游 MCP server（三個 tool）
├── litellm/                 # config.yaml（模型別名、fallback、MCP server、OBO、pinned_tools、guardrail）；config.fallback.yaml、config.spike.yaml 只用於驗證
├── keycloak/                # realm-governance.json（測試帳號與 client；kyt-cli 的 direct access grants 供 kyt_agent 使用）
├── probe_mcp/               # 測試用上游 MCP server
├── probe_llm/               # 測試用的 OpenAI 相容 provider（綁 127.0.0.1:8300），記錄請求並依 probe: 指令回應
├── scripts/                 # M1 驗證腳本、agent_sim、llm_sim、demo_m2、demo_m4、make_keys
│   └── spikes/              # M4 實作前的 hook 驗證（m4_spike、m4_hook_probe）
├── tests/                   # 單元測試；integration/ 需要 docker compose 服務
├── docs/                    # specs/、plans/、spikes/、images/architecture.svg（README 最上方的能力架構圖，新增或移除能力時同步編輯）
└── var/                     # 執行產物，含 keys.json（不進版控）
```

LiteLLM 固定在 v1.105.1：tool 定義鎖定從此版起才有；`plugin.py` 依賴 `CustomGuardrail` 介面、`litellm.proxy.proxy_server.app`（含已建好的 `middleware_stack`）與 hook 的 `mcp_*` 欄位。
LLM guardrail 另外依賴 `call_type` 的值（`acompletion`）、`litellm_call_id` 在 pre-call 與 post-call 之間不變、`proxy_server_request.body`，以及 streaming 時不呼叫 success hook（`docs/spikes/m4-findings.md` A1–A6）。
**升級 LiteLLM 前必須跑 `uv run pytest -m integration`**（含 gateway 與 LLM guardrail 的契約測試），結果須為 31 passed 而非 skipped，並跑 `uv run python -m scripts.demo_m4` 確認 13/13。
升級後也要重新比對 Presidio 的相依：映像內 `pip freeze` 的既有套件不能被改版。

`engine/` 不 import LiteLLM、FastAPI 與 `config`；依賴方向為 `plugin` / `api` → `runtime` → `engine` / `config`，`runtime` 負責把設定注入引擎。

## 工具鏈

- **Python**：3.12 以上
- **套件管理**：`uv`（`uv add`、`uv run`），不直接使用 `pip install`。唯一的例外是 `Dockerfile`：LiteLLM 映像沒有 pip 與 uv，以 `ensurepip` 暫時安裝 pip，裝完 `presidio-analyzer` 後移除。版本與 `pyproject.toml` 的 dev group 一致（目前 2.2.364），新增的套件以 `guardrails/presidio-constraints.txt` 鎖定；升級 Presidio 時兩處一起改，並以 `pip freeze` 前後比對重產 constraints。不安裝 spaCy 語言模型，程式只直接使用個別 recognizer
- **Lint / Format**：`ruff`，line-length 100
- **型別檢查**：`mypy src`，公開函式必須有型別標註
- **測試**：`pytest`；需要 Docker 服務的測試標記 `@pytest.mark.integration`，預設不執行
- **設定**：gateway 內的 `Settings` 以 `pydantic-settings` 從容器環境變數讀取 `GOV_*`（不讀 `.env`）；主機上的 scripts 與測試以 python-dotenv 載入 `.env`；程式中不得寫死任何 key
- **Policy**：Rego v1（OPA 1.21.1）；修改 `policies/` 要更新 `meta/data.json` 的 `policy_version` 並通過 `opa test`
- **注入規則**：修改 `guardrails/injection_rules.json` 要更新 `rules_version`，並通過 `uv run pytest tests/test_guardrail_injection.py`（含 kyt 工具輸出的誤判語料與 20 萬字元的計時測試）；新規則不得出現會平方回溯的寫法（行首用 `[ \t]*`，間隔要有上限）
- **服務**：`docker compose` 啟動 gateway、Postgres、Keycloak、OPA、account_ops、probe_mcp、probe_llm；image 一律固定版本

```bash
uv sync
docker compose up -d --build
uv run pytest
uv run pytest -m integration
uv run python -m scripts.demo_m4
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
- PII 對照表（`PseudonymMap`）只存在 gateway process 的記憶體，不得放進 LiteLLM 的 `data` / `metadata`（會被 callback 記錄）、稽核或日誌；guardrail 的錯誤日誌只記例外類別名稱
- `llm_guardrail` 稽核只記 PII 類型與數量、規則編號與內容的 sha256，不記原值與 tool 原文
- 新增 LLM 請求欄位前，確認它會被遮蔽或屬於開發者定義的內容，再加入 `engine/guardrails/fields.py` 的白名單
- 受治理的 key 一律設 `allowed_routes`（`scripts/make_keys.py` 的 `LLM_ROUTES`、`MCP_ROUTES`）：provider pass-through、files、threads 不經過 pre-call hook，沒有限制路由就能繞過 guardrail

## 已知問題

- gateway 以多個 worker 首次啟動時，各 worker 的 `ensure_schema()` 可能同時執行 `CREATE DATABASE` 而衝突（M2 起就有，目前單一 worker 不受影響）
