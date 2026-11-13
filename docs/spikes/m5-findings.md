# M5 實作前驗證結果（2026-11-13）

- 對應：[spike 計畫與靜態檢查](m5-user-budget-spike.md)（L1–L10）、[M5 設計](../specs/2026-11-13-user-budget-design.md)、[實作計畫](../plans/2026-11-13-user-budget.md) Task 1
- 環境：LiteLLM v1.105.1（gateway 映像），`probe_llm`，Keycloak 測試帳號 alice
- 方法：`docker compose -f docker-compose.yml -f docker-compose.spike-m5.yml up -d --build --no-deps gateway probe_llm`，執行 `uv run python -m scripts.spikes.m5_spike`；輸出存於 `var/m5-spike.txt`（不進版控）。驗證完以一般設定重建 gateway 與 probe_llm，臨時 key 皆已刪除
- 觀察來源：`scripts/spikes/m5_budget_probe.py` 寫的 `M5SPIKE` 日誌（只記鍵名、布林、數字與雜湊前 8 碼）、`probe_llm` 的請求與 header 紀錄、`LiteLLM_SpendLogs`
- B7 只有第二階段需要，B8 在 Task 13 量測，本次都沒有執行

## 結論

| 編號 | 結果 | 證據 |
| ---- | ---- | ---- |
| B1 | 成立 | 非 streaming 與 streaming 的 pre-call 都有 `data["secret_fields"]`（`dict`），其中 `raw_headers` 含 `authorization` 與 `x-litellm-api-key`，`authorization` 的值是 JWT |
| B2 | 成立 | 以 `x-litellm-api-key` 認證成功（200）；`probe_llm` 收到的 `authorization` 不是 JWT（是 deployment 的 api key），其他 header 也沒有 JWT |
| B3 | 成立，有兩點要注意 | 每個請求只有一次 `async_log_success_event`：非 streaming 在 `success_hook` 之後，streaming 在 `stream_end` 之後，fallback 只有成功的那次（`model` 為 `openai/probe-fallback`、`model_group` 為 `probe-fallback`）。`kwargs["litellm_call_id"]` 與 pre-call 相同；`standard_logging_object` 的 `id` **不是** call_id，要用 `kwargs["litellm_call_id"]`（`standard_logging_object` 也有 `litellm_call_id` 鍵）。`prompt_tokens`、`completion_tokens` 與 `LiteLLM_SpendLogs` 相同。`metadata.user_api_key_hash`、`user_api_key_dict.token` 與 `SpendLogs.api_key` 三者同值。**成本是有二進位誤差的 float**：`response_cost` 為 `0.00012200000000000001`，`SpendLogs.spend` 為 `0.000122` |
| B4 | 與假設不同，不影響設計 | 主力回 503 兩次後改走 fallback，`async_log_failure_event` 只觸發一次；`async_post_call_failure_hook` 沒有觸發。設計本來就不用 `log_failure` 釋放 |
| B5 | 成立 | 以 `tpm_limit=1` 的臨時 key 送出：rate limit 觸發 fallback 時，pre-call 以兩個 `call_id` 各跑一次；第一個收到 `log_failure` 與 failure hook，回應的 `x-litellm-call-id` 也是第一個；第二個沒有任何後續事件，`LiteLLM_SpendLogs` 也**沒有紀錄**。另外，這把 key 的第一個請求就被 429（M4 A8 以 `tpm_limit=1000` 測時第一個會放行），推測限流器在呼叫前也會計入輸入的估計，未深入確認 |
| B6 | 有事件，成本只含已產生的部分 | streaming 讀到第一個 chunk 後關閉連線：`stream_end`（1 個 chunk）之後仍有 `log_success`，成本只含輸入（`completion_tokens` 為 0），`SpendLogs` 為 `success`。probe 在中斷後不再產生內容；真實 provider 中斷後可能已經產生並計費更多輸出，帳本會少記這部分 |
| B9 | 成立，但重置時間是日曆月 | `budget_duration: "1mo"` 被接受，`budget_reset_at` 為 `2026-12-01T00:00:00+00:00`：從下個月 1 日 00:00 UTC（台北 08:00）重置，不是從建立時間起算 30 天 |
| B10 | 成立 | pre-call 改寫的 `max_tokens`（17）送到 provider；router fallback 的三次請求（主力兩次、fallback 一次）都是 17 |

## 對後續 task 的影響

- **Task 3、9（token）**：從 `data["secret_fields"]["raw_headers"]` 取 `authorization`，名稱要不分大小寫比對；不需要替代方案
- **Task 9（結算）**：call_id 取 `kwargs["litellm_call_id"]`，不要用 `standard_logging_object["id"]`。成本先 `round(value, 12)` 再轉 `Decimal`，帳本的成本才會與 `SpendLogs.spend` 相同（probe 單價下兩者在第 12 位以內一致）。`owner` 以 `sha256_hex(metadata.user_api_key_hash)` 計算，與 `owner_of()` 相同
- **Task 10（對帳）**：`SpendLogs.api_key` 就是 key 的雜湊，`sha256_hex(api_key)` 等於帳本的 `owner`。rate limit fallback 的第二個 `call_id` 在 SpendLogs 沒有任何紀錄，若照原設計「都沒有就保留 `in_flight`」，每次 429 都會留下一筆永遠無法解決的紀錄。改為：放行超過 `--release-missing-after`（預設 1 小時）仍查不到任何 SpendLogs 時標為 `released`，在輸出中分開計數（`released_missing`）
- **Task 11（key）**：`budget_duration: "1mo"` 可用；重置時間是每月 1 日 00:00 UTC
- **Task 12（契約測試）**：B6 的中斷情境預期為「結算、成本只含輸入」；fallback 情境的 `log_failure` 只有一次
- **Task 14（README）**：已知限制加入「streaming 中斷時只記到中斷前的用量，真實 provider 可能計費更多」
- **spec 修訂**：§6.3 的「不是日曆月」改為「每月 1 日 00:00 UTC」；§8.5 加入查不到 SpendLogs 時的釋放規則；§11 標記 B1–B6、B9、B10 已驗證

## 本次新增的驗證工具

- `probe_llm`：`GET /request-headers` 記錄每個請求的 header 名稱、`authorization` 是否為 JWT、哪些 header 含 JWT（不記值），`DELETE /requests` 一併清除；`probe:chunk_delay=<秒>` 讓 streaming 每個 chunk 前等待。之後的契約測試（B2、B6）也會用到
- `litellm/config.spike-m5.yaml`、`docker-compose.spike-m5.yml`：與當時的 `config.yaml` 相同，只把 `llm-guardrail` 換成只記錄的 `M5SpikeGuardrail`，probe 模型加上測試單價。計畫原本寫修改 M4 的 `config.spike.yaml`，改為另建檔案，保留 M4 的重現環境

## 真實 LLM 驗證（2026-11-14，實作計畫 Task 13）

在 M5 實作完成後，以 gateway（本分支的映像）、alice 的 token 與 `kyt-agent@1.5` key 呼叫 `kyt-main`（`gemini/gemini-3.8-flash`）。輸出存於 `var/m5-real-check.txt`（不進版控），共 5 次呼叫，實際花費 $0.0523（略高於計畫估計的 $0.05，因為最大的請求有 6 萬 token）。

### 結算與真實單價

| 請求 | 帳本狀態 | 帳本成本 | `SpendLogs.spend` | 一致 |
| ---- | -------- | -------- | ----------------- | ---- |
| 40 個 tool 結果 | settled（callback） | 0.00591075 | 0.00591075 | 是 |
| 140 個 tool 結果 | settled（callback） | 0.016667025 | 0.016667025 | 是 |
| 300 個 tool 結果 | settled（callback） | 0.02777145 | 0.02777145 | 是 |
| 長篇輸出，`max_tokens=256` | settled（callback） | 0.0009615 | 0.0009615 | 是 |
| 同上 | settled（callback） | 0.0009615 | 0.0009615000000000001 | 是（四捨五入到第 12 位後相同） |

### B8：字元數與 token 數

請求內容取自 `tests/data/kyt_tool_outputs.jsonl`（kyt audit log 的真實工具輸出：hex 地址、交易對手清單、中文說明），以 assistant tool call 加 tool 結果的形式組成，`text_length()` 計算的字元數與 Gemini 回報的 `prompt_tokens`：

| tool 結果數 | 字元數 | prompt_tokens | 字元／token |
| ----------- | ------ | ------------- | ----------- |
| 40 | 7,670 | 7,821 | 0.98 |
| 140 | 28,474 | 28,757 | 0.99 |
| 300 | 62,079 | 62,336 | 1.00 |

- **B8 不成立，但差距很小**：kyt 的內容幾乎一個字元一個 token，token 數約為字元數的 1.00–1.02 倍（多出的部分包含 guardrail 加上的 spotlighting 邊界與角色標記）
- 對設計的影響：只有 spec §8.6 的超額上界要乘上 1.02。`max_input_chars` 100,000 約等於 102,000 個輸入 token，單筆最大成本約 $0.092，`max_concurrent` 4 時上界約 $0.37
- 這個比例只代表 kyt 的內容；英文為主的內容通常是 3–4 字元／token，上界反而更寬鬆

### B7（順帶）：reasoning token 計入 `max_tokens`

兩次 `max_tokens=256` 的長篇輸出：`completion_tokens` 252，其中 `reasoning_tokens` 245 與 243，`finish_reason` 為 `length`。Gemini 的 `max_tokens` 涵蓋 reasoning token，第二階段的預留不需要另加 reasoning 上限。第一階段的影響：輸出上限設太低時，可見的回答可能幾乎被 reasoning 用完；kyt 的上限 4,096 遠高於實測最大輸出 1,806，不受影響。
