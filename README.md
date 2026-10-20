# AI Governance Gateway

統一治理 agent 的 LLM 流量與 tool 呼叫：agent 身份、委託授權、policy 判斷、人工審批與稽核。
以 LiteLLM 作為 LLM 與 MCP 的 data plane，Keycloak 作為 IdP；治理邏輯只補 LiteLLM 開源版做不到的部分。

## 現況

| 階段 | 內容 | 狀態 |
| ---- | ---- | ---- |
| M1 | 技術驗證：LiteLLM（LLM + MCP gateway）、Keycloak、OBO | 完成，結果見 [m1-findings.md](docs/spikes/m1-findings.md) |
| M2 | 治理服務：OPA policy、maker-checker 審批、稽核 log、高風險 tool | 未開始 |

## 架構

```
client（kyt_agent / scripts）
  │  x-litellm-api-key：agent 的 virtual key
  │  Authorization：使用者的 Keycloak token
  ▼
LiteLLM v1.105.1
  ├─ LLM：模型別名 kyt-main ──▶ Gemini（rpm、預算、依 key 計費）
  └─ MCP：tool 權限、tool 定義鎖定
        ├─ governance hook（pre_mcp_call）：取得 agent、使用者、tool、參數，可阻擋或回傳 pending
        └─ OBO：向 Keycloak 交換 audience 縮限的 token ──▶ 上游 MCP server（probe_mcp）
```

M1 的主要結論：

| 能力 | 由誰提供 |
| ---- | -------- |
| 模型別名、rpm、預算、依 key 計費 | LiteLLM 開源版 |
| MCP tool 權限、tool 定義鎖定、對上游的 OBO | LiteLLM 開源版（tool 鎖定需 v1.105.1 以上） |
| 依參數值的 policy、maker-checker 審批、防竄改稽核 | 自建（M2），以 LiteLLM hook 為執行點 |

上游 MCP server 只看得到交換後的使用者 token，看不到是哪個 agent 發起，所以治理執行點放在 LiteLLM hook，而不是上游。

## 服務

| 服務 | Port | 用途 |
| ---- | ---- | ---- |
| `litellm` | 4000 | LLM 與 MCP gateway |
| `postgres` | - | LiteLLM 的 virtual key 與用量 |
| `keycloak` | 8080 | IdP，realm `governance`（啟動時匯入 `keycloak/realm-governance.json`） |
| `probe_mcp` | 8100 | 測試用上游 MCP server，回傳收到的 header 與 token claims |

## 啟動

```bash
cp .env.example .env   # 填入 LITELLM_MASTER_KEY（sk- 開頭）、POSTGRES_PASSWORD、GOOGLE_API_KEY、KEYCLOAK_ADMIN_PASSWORD
uv sync
docker compose up -d
uv run python -m scripts.make_keys        # 建立 virtual key，寫入 var/keys.json
uv run python -m scripts.call_whoami      # 以 alice 身份經 LiteLLM 呼叫 probe 的 whoami
```

Keycloak 測試帳號 `alice`（analyst）、`bob`（compliance_supervisor）與各 client secret 都是 `keycloak/realm-governance.json` 中的測試值，只供本機使用。腳本只輸出 token 的 claims，不輸出原文。

## 驗證腳本

| 指令 | 驗證內容 |
| ---- | -------- |
| `uv run python -m scripts.show_claims alice` | 使用者 token 的 claims |
| `uv run python -m scripts.call_whoami --user alice` | OBO 與上游可見的身份（`--user ''` 不帶使用者 token） |
| `uv run python -m scripts.call_echo` | governance hook 依參數值阻擋 |
| `uv run python -m scripts.rate_limit_check` | virtual key 的 rpm 限制 |
| `uv run python -m scripts.exchange_with_actor` | Keycloak delegation（需開啟 preview feature，見 findings V5） |

kyt_agent 經 LiteLLM 呼叫模型：

```bash
LLM_BASE_URL=http://localhost:4000 LLM_API_KEY=<kyt-agent virtual key> \
  LLM_PRICE_MODEL=google_genai:gemini-3.8-flash \
  uv run kyt eval --model openai:kyt-main --limit 3
```

## 開發

```bash
uv run pytest
uv run ruff format . && uv run ruff check .
```

## 文件

- 設計：[docs/specs/](docs/specs/)
- 實作計畫：[docs/plans/](docs/plans/)
- 技術驗證結果：[docs/spikes/m1-findings.md](docs/spikes/m1-findings.md)
