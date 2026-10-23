# M4 實作前驗證結果（2026-10-23）

驗證環境：LiteLLM `v1.105.1`（`docker.io/litellm/litellm`）、`probe_llm`、`litellm/config.spike.yaml`（在 `governance` 之後加上 `m4-spike` guardrail）。
重現方式：

```bash
docker compose -f docker-compose.yml -f docker-compose.spike.yml up -d --force-recreate gateway
uv run python -m scripts.spikes.m4_spike
docker compose logs gateway --no-log-prefix | grep M4SPIKE
docker compose up -d --force-recreate gateway   # 還原
```

hook 的觀察來自 `scripts/spikes/m4_hook_probe.py` 寫的 `M4SPIKE` 日誌（含跨 hook 的遞增序號 `seq`）；以下只摘錄結論，不貼日誌原文。
spec §4 的 S1–S5 對應下表：S1 為「S1 / Presidio 打包」與「S1 / tldextract」，S2–S5 分別對應 A2、A3、A4（含 A1 的 `call_type`）、A8；計畫標頭提到的「S6」指「其他觀察」中額外的 hook 檢查（streaming 與 success hook 的順序、MCP、failure hook 等）。

## 結論

| 編號 | 結論 | 觀察 |
| ---- | ---- | ---- |
| S1 / Presidio 打包 | 通過 | 映像為 Wolfi（glibc 2.44）、Python 3.13.16、venv `/app/.venv`，有 `ensurepip`（pip 26.2.1）；pydantic 2.13.4、numpy 2.4.4 符合 presidio 的需求。採 `ensurepip` → `pip install presidio-analyzer==2.2.364`（2026-07-22 發布）→ 移除 pip。`pip freeze` 前後比對：既有套件版本完全不變，只新增 spaCy 3.8.16、thinc、tldextract 5.4.0、phonenumbers 等（不含任何 spaCy 語言模型），`pip check` 無衝突。映像 1.73 GB → 2.03 GB（壓縮後 411 MB → 469 MB，+58 MB）。容器內 `EmailRecognizer` / `CreditCardRecognizer` 的 `analyze(text, entities, None)` 印出 `['EMAIL_ADDRESS']`、`['CREDIT_CARD']`；M2 整合測試 17 passed、`demo_m2` 13/13。Presidio 新增的 22 個套件已以 `guardrails/presidio-constraints.txt` 鎖定版本（`pip install -c`），重建後以 `importlib.metadata` 比對，套件與版本與驗證時相同 |
| S1 / tldextract | 通過，但需處理 | `import presidio_analyzer` 約 1 秒（會載入 spaCy 套件）。`EmailRecognizer` 第一次驗證網域時，tldextract 會向 `publicsuffix.org` 下載 Public Suffix List（gateway 可連外：0.32 秒，之後快取在 `/root/.cache/python-tldextract`）；`--network none` 與 internal 網路下 DNS 立即失敗，退回內建 snapshot（0.05 秒，不會卡住），但會印出兩段 traceback。下載未設 timeout（`TLDEXTRACT_CACHE_TIMEOUT` 未設），網路黑洞時可能卡住。改成 `tldextract.tldextract.TLD_EXTRACTOR = TLDExtract(suffix_list_urls=())` 後只用 snapshot，離線 0.045 秒、無 traceback、結果相同 |
| A1 | 成立（只有 `"acompletion"`） | chat 不論 streaming 與否 `call_type` 都是 `"acompletion"`；embeddings 也會經過 `pre_call`，`call_type` 是 `"aembedding"`。MCP tool call **不會**呼叫 `mode: pre_call` guardrail 的 `async_pre_call_hook`（跑 `demo_m2` 期間沒有任何 `pre_call` 紀錄） |
| A2 | 成立 | provider 收到 `hello <SPIKE_1>`。router 層的 fallback（primary 回 503，`num_retries: 1`）：provider 依序收到 primary、primary、fallback 三次，內容都是改寫後的 `<SPIKE_1>`；`pre_call` 只跑一次、`post_call` 一次，`call_id` 相同 |
| A3 | 成立 | `async_post_call_success_hook` 的 `response` 是 `ModelResponse`，直接改 `choices[i].message.content` 與 `tool_calls[j].function.arguments` 後，client 收到還原後的 `hello SPIKE_SECRET` 與 `{"text": "to \"SPIKE_SECRET\""}`；fallback 的回應同樣經過此 hook |
| A4 | 成立（需跨 chunk 暫存） | streaming 請求經過 `pre_call` 改寫，回應經過 `async_post_call_streaming_iterator_hook`，chunk 可改寫：佔位符完整落在同一 chunk 時還原成功；切在佔位符中間（`probe:split=6`）時逐 chunk 取代失敗，client 收到 `ab <SPIKE_1>`。streaming 時 **不會** 呼叫 `async_post_call_success_hook`（順序為 `pre_call` → `stream` → `stream_end`） |
| A5 | 成立（格式由 LiteLLM 包裝） | 非 streaming 與 streaming 請求都回 HTTP 403（`pre_call` 在開始串流前執行）。body 為 OpenAI 格式：`error.type="permission_error"`、`error.code="403"`，`detail` 以 JSON 字串放在 `error.message`，並複製到 `error.provider_specific_fields`；LiteLLM 會另外加上 `guardrail_name`、`guardrail_mode` |
| A6 | 成立（router fallback）；有例外 | 同一請求的 `pre_call` 與 `post_call` / `stream` / `stream_end` 的 `litellm_call_id` 相同，router 層 fallback 也不變。例外見下方「rate limit 觸發的 fallback」。`data` 的鍵：`litellm_call_id`、`litellm_logging_obj`、`messages`、`metadata`、`model`、`proxy_server_request`、`secret_fields`、`stream`；`metadata` 含 `user_api_key_alias`、`user_api_key_hash`、`headers` 等 |
| A7 | key 不需要列出 fallback 模型 | 新建只含 `["probe-primary"]` 的 key，primary 回 503 時 provider 收到 primary、primary、fallback，client 拿到 fallback 的 200 回應。router fallback 不檢查 key 的 `models` |
| A8 | 事後累計、事前比對 | guard-tpm（`tpm_limit=1000`）每次回報 5000 token：結果為 200、429、429。第一次即使超過上限也放行，用量在回應後累計；之後的請求在呼叫 provider 前就被擋（provider 沒收到請求）。429 訊息為 `Rate limit exceeded ... Limit type: tokens. Current limit: 1000`，沒有 `x-ratelimit-remaining-tokens` header |

## 其他觀察

- **mode 語法**：`mode: [pre_call, post_call]`（YAML list）可直接使用，LiteLLM 不會拒絕；403 的回應中 `guardrail_mode` 為 `["pre_call", "post_call"]`
- **rate limit 觸發的 fallback**：key 的 tpm / rpm 超限時（`ProxyRateLimitError`），proxy 的 `_pre_call_with_fallbacks` 會以請求的**原始快照**換成 fallback 模型，重跑整個 pre-call 流程。guardrail 的 `async_pre_call_hook` 因此會再跑一次，`litellm_call_id` 是**新的**，看到的是原文而非改寫後的內容。guardrail 在 rate limiter 擋下之前就已執行，所以同一個被擋的請求會讓 guardrail 跑兩次（primary、fallback 各一次），最後回 429
- **`async_post_call_failure_hook`**：自己丟的 403、provider 錯誤（404）、429 都會呼叫，但 429 只用**第一次**的 `call_id` 呼叫一次，fallback 那次的 `call_id` 不會收到 success 也不會收到 failure。跑 `demo_m2` 時，MCP 被 M2 擋下（`GovernanceBlocked`）的 13 次也會呼叫這個 hook，不論 guardrail 的 mode
- **client 無法關閉 guardrail**：請求 body 帶 `metadata.disable_global_guardrails`、`metadata.opted_out_global_guardrails`、`guardrails: []` 時，guardrail 仍會執行（LiteLLM 只從 key / team 的管理端 metadata 讀取這兩個設定）
- **deployment cooldown**：probe_llm 沒有 `/v1/embeddings`，回 404 後 `probe-primary` 進入 5 秒 cooldown，期間的請求不送 primary、直接走 fallback（第一次執行時 A7 因此只看到 fallback）。腳本在 fallback 相關檢查之間等待 6 秒，embeddings 放在最後
- **驗證留下的 key**：`m4_spike.py` 每次執行會在 LiteLLM 建立一把 `spike-primary-only-<時間戳>` key（本次驗證共建立 3 把），只用於 A7，可以刪除
- **主機環境**：主機的 venv 是 Python 3.14.3，`uv sync` 安裝 presidio-analyzer 2.2.364 與 spaCy 3.8.16，兩個 recognizer 可正常使用（首次 import 約 2 秒）

## 對後續 task 的影響

- **Task 4 / 5（`pii.py`）**：在模組載入時把 tldextract 換成只用內建 snapshot（`tldextract.tldextract.TLD_EXTRACTOR = TLDExtract(suffix_list_urls=())`），避免 gateway 對外連線、沒有 timeout 的下載與離線時的 traceback；單元測試加一項：`--network none` 或 mock 下 `EmailRecognizer` 仍正確
- **Task 8 `CHAT_CALL_TYPES`**：`{"acompletion"}` 即可（保留 `"completion"` 無害）。`"aembedding"` 會進入 `pre_call`，依設計拒絕或放行非 chat 的呼叫；MCP 不會進入這個 hook，不需要排除 `call_mcp_tool`
- **Task 8 對照表的生命週期**：
  - 非 streaming 在 `async_post_call_success_hook` 取出並刪除；streaming 在 `async_post_call_streaming_iterator_hook` 結束（含例外）時刪除。streaming 不會呼叫 success hook，所以在 success hook pop 不會影響 streaming
  - 另實作 `async_post_call_failure_hook` 刪除對照表；找不到 `call_id` 時必須 no-op（MCP 被擋的請求也會呼叫它）
  - rate limit fallback 會產生收不到任何後續 hook 的 `call_id`：對照表必須有 TTL 或數量上限，不能只靠 post-call 刪除
  - 稽核：一個被 rate limit 擋下的請求會讓 pre-call 的處理與稽核發生兩次（各自的 `call_id`），demo 與整合測試的稽核筆數要考慮這點
- **Task 8 streaming 還原**：佔位符可能跨 chunk，`StreamTextRestorer` 必須暫存可能是佔位符開頭的尾端文字，於後續 chunk 或串流結束時輸出
- **Task 8 錯誤回應**：client 看到的不是原始的 `detail`，而是 OpenAI 格式的 `error`；demo 與整合測試以 `status_code` 和 `error.provider_specific_fields` 判斷
- **Task 9 fallback**：router 層 fallback 沿用 pre-call 改寫後的 `messages`，不需要在 fallback 前再跑 guardrail。key 的 `models` 不必列出 fallback 模型；`make_keys.py` 的 `kyt-agent@llm-demo`、`guard-strict` 列出兩個模型無害，可保留
- **Task 9 / 10 tpm**：`tpm_limit` 是事後累計、事前比對，單一大請求會超過上限仍被放行；文件與 demo 的預期為「第一次 200、之後 429」。若有設定 fallback，429 前 proxy 會先嘗試 fallback 模型（同一個 key 的 tpm 仍超限，因此仍為 429）
- **整合測試與 demo**：probe 回 404 或 503 後，該 deployment 有 5 秒 cooldown；fallback 相關的情境之間要等待或改用環境變數 `PROBE_FAIL_MODELS`，否則 primary 可能被跳過
- **Task 12（spec 修訂）**：§3 的「spaCy 最小的英文模型」改為「只安裝 presidio-analyzer 與 spaCy 套件，不下載語言模型」，並記錄映像增加約 300 MB（壓縮後 58 MB）
