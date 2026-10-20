# AGENTS.md

AI Governance Gateway：統一治理 agent 的 LLM 流量與 tool 呼叫，提供 agent 身份、委託授權、policy 判斷、人工審批與稽核。
以 LiteLLM 作為 LLM 與 MCP 的 data plane，治理邏輯以 Python 實作。
設計文件在 `docs/specs/`，實作計畫在 `docs/plans/`，技術驗證結果在 `docs/spikes/`。

## 開發階段

| 階段 | 內容 | 狀態 |
| ---- | ---- | ---- |
| M1 | 技術驗證：LiteLLM（LLM + MCP gateway）、Keycloak、OBO，決定 M2 架構 | 進行中 |
| M2 | 治理服務：OPA policy、maker-checker 審批、稽核 log、高風險 tool | 未開始 |

## 工具鏈

- **Python**：3.12 以上
- **套件管理**：`uv`（`uv add`、`uv run`），不直接使用 `pip install`
- **Lint / Format**：`ruff`，line-length 100
- **型別檢查**：`mypy src`，公開函式必須有型別標註
- **測試**：`pytest`；需要 Docker 服務的測試標記 `@pytest.mark.integration`，預設不執行
- **設定**：`pydantic-settings` 讀取 `.env`，程式中不得寫死任何 key
- **服務**：`docker compose` 啟動 LiteLLM、Postgres、Keycloak；image 一律固定版本

```bash
uv sync
docker compose up -d
uv run pytest
uv run ruff format src tests && uv run ruff check src tests && uv run mypy src
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
