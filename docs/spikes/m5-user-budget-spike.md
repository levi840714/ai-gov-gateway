# M5 依使用者預算：LiteLLM v1.105.1 的驗證

- 日期：2026-11-13
- 狀態：靜態檢查已完成（L1–L10）；執行期驗證（B1–B10）待實作計畫 Task 1 執行，結果寫入 `docs/spikes/m5-findings.md`
- 對應設計：[M5 依委託使用者限制 LLM 用量](../specs/2026-11-13-user-budget-design.md)

## 目的

決定「依委託使用者的預算」要交給 LiteLLM 的 end user（customer）預算（spec 方案 A），還是由治理引擎自己累計（方案 B）。
先讀 gateway 映像內的 LiteLLM 原始碼確認機制是否存在、在請求流程的哪一步執行；需要看實際行為的項目列在 B1–B10，由實作的第一個 task 以 probe 驗證。

## 靜態檢查（已完成）

讀的是執行中的 `ai-gov-gateway-gateway-1` 容器（映像 `litellm/litellm:v1.105.1`）內 `/app/.venv/lib/python3.13/site-packages/litellm/` 的原始碼，只讀不改，也沒有送出請求。
下表的路徑都相對於這個目錄，行號以 v1.105.1 為準。

| 編號 | 結論 | 出處 |
| ---- | ---- | ---- |
| L1 | end user 預算存在。`_check_end_user_budget` 與 key、team、user 的預算一起在 `common_checks` 檢查，超過時丟 `BudgetExceededError`。比較式是 `spend > max_budget`；user 預算是 `>=` | `proxy/auth/auth_checks.py:1231`、`:1644–1684`、`:1170` |
| L2 | end user id 在 `user_api_key_auth` 決定，來源依序為：標準 header `x-litellm-customer-id`、`x-litellm-end-user-id`（不需設定，一律檢查）→ `user_header_mappings` / `user_header_name` → body 的 `user` → `litellm_metadata.user` → `metadata.user_id` → `safety_identifier`。**全部都由 client 提供** | `proxy/auth/user_api_key_auth.py:1902`、`proxy/auth/auth_utils.py:1595–1677`、`constants.py:1585` |
| L3 | 預設不檢查 end user 是否存在（`litellm.validate_end_user_id_in_db` 預設為 False），任何字串都會被接受。設定了預設預算（`max_end_user_budget_id` 或 key 的 `end_user_budget_id`）時，每個沒看過的 id 都套用一份新的預設額度，**換一個 id 就有新的額度** | `proxy/auth/auth_checks.py:1981–2041`、`:1446–1600` |
| L4 | 執行順序是 auth（FastAPI dependency，含預算檢查）→ `add_litellm_data_to_request`（把 auth 得到的 id 寫進 `metadata.user_api_key_end_user_id`）→ `proxy_logging_obj.pre_call_hook`（guardrail 在這一步）。花費歸屬取 `user_api_key_end_user_id`，不讀 `data["user"]`。**在 guardrail 覆寫 `user` 時，預算已經用 agent 自填的 id 檢查完，花費也記在那個 id** | `proxy/common_request_processing.py:2034`、`:2211`；`proxy/litellm_pre_call_utils.py:1642`；`utils.py:10070–10096` |
| L5 | end user 的 rpm、tpm、tpd 由 budget table 帶進 `UserAPIKeyAuth`，限流器依 `user_api_key_dict.end_user_id` 計數，一樣綁在 auth 階段的 id | `proxy/auth/user_api_key_auth.py:613–633`；`proxy/hooks/parallel_request_limiter_v3.py:3213` |
| L6 | end user 花費的計數鍵是 `spend:end_user:<id>`，回應後才累計，與 key 預算同樣「事後累計、事前比對」（M4 findings A8） | `proxy/auth/auth_checks.py:1668–1674` |
| L7 | budget table 有 `budget_duration`，reset job 也會重置 end user 的花費 | `proxy/common_utils/reset_budget_job.py:203–261` |
| L8 | pre-call hook 拿得到原始 header：`data["secret_fields"]["raw_headers"]`（`SecretFields` 是 TypedDict，註明不進 logging）；`proxy_server_request.headers` 則已經遮蔽憑證 | `proxy/litellm_pre_call_utils.py:2078`、`:2175`；`types/proxy/litellm_pre_call_utils.py:17` |
| L9 | 認證時 `x-litellm-api-key` 優先於 `Authorization`，所有路由都一樣，與 MCP 路徑（kyt v1.5）的用法相同 | `proxy/auth/user_api_key_auth.py:823–828` |
| L10 | 價格表：`gemini/gemini-3.8-flash` 輸入 7.5e-7、輸出 3.75e-6、reasoning 3.75e-6（每 token 美元），`max_output_tokens` 65,536；`gpt-6-luna` 輸入 1e-7、輸出 5e-7，`max_output_tokens` 128,000（查 `openai/gpt-6-luna` 查不到，要去掉 provider 前綴）。probe 模型沒有價格 | 容器內 `litellm.model_cost` |

### 對方案 A 的影響

- 原本的做法是「在 pre-call hook 用 token subject 覆寫 `user`」，依 L4 不會生效：預算檢查與花費歸屬都在 hook 之前就用 client 自填的 id 決定了
- 被注入的 agent 可以在 header 或 body 帶任意 id（L2），沒有預設預算時等於不受限，有預設預算時換 id 就得到新額度（L3）
- 要讓 A 可行，必須在 LiteLLM 認證之前（ASGI middleware）驗證 token、移除 client 帶的所有 end user 來源、再補上可信的 `x-litellm-end-user-id`。即使如此，仍然是事後累計（L6），並行請求沒有預留，拒絕時也不會寫進我們的 hash chain。比較見 spec §5

## 執行期驗證（待 Task 1）

做法與 M4 相同：以 `scripts/spikes/m5_budget_probe.py` 註冊一個只記錄的 guardrail（`litellm/config.spike.yaml` 疊加），對 `probe_llm` 送請求，從日誌與 `probe_llm` 的請求紀錄判斷。任一項不成立時停下來回報，由 controller 調整設計，不要自行改。

| 編號 | 要驗證的事 | 用在 | 不成立時 |
| ---- | ---------- | ---- | -------- |
| B1 | key 放 `x-litellm-api-key`、使用者 token 放 `Authorization` 時，`LlmGuardrailHook.async_pre_call_hook` 的 `data["secret_fields"]["raw_headers"]` 含 `authorization`，非 streaming 與 streaming 都一樣 | spec §4.2 | 改用自訂 header `x-gov-user-token`，由 ASGI middleware 取出放進 request state |
| B2 | 同上的請求由 `x-litellm-api-key` 認證成功；`Authorization` 不會被轉送給 provider（`probe_llm` 記錄的 header 沒有 JWT）；`AuthorizationHeaderGuard` 不擋 | spec §4.2 | 同 B1 的替代方案 |
| B3 | guardrail 類別（`CustomGuardrail` 繼承 `CustomLogger`）的 `async_log_success_event` 會被呼叫：kwargs 的 `litellm_call_id` 與 pre-call 相同；`kwargs["response_cost"]` 或 `standard_logging_object["response_cost"]` 與 `LiteLLM_SpendLogs.spend` 相同；streaming 在串流結束後呼叫一次；router fallback 時只對成功的那次呼叫一次；拿得到 key 的雜湊（與 `owner_of` 相同的來源） | spec §8.3 | 另寫一個只做結算的 `CustomLogger`，在 `litellm_settings.callbacks` 註冊 |
| B4 | `async_log_failure_event` 在 router 重試、fallback 前的每次失敗是否都會呼叫 | spec §8.3：確認不能用它釋放預留 | 無影響（設計只用 `async_post_call_failure_hook` 釋放） |
| B5 | rate limit 觸發的 fallback：第二個 `call_id` 不會收到任何 success / failure 事件 | spec §8.4 | 改由該事件釋放，縮短 TTL |
| B6 | streaming 中途 client 斷線時，是否有 success 或 failure 事件、cost 是否為已產生的部分 | spec §8.3 | 列為已知限制，靠 TTL 回收 |
| B7 | Gemini 的 `max_tokens` 是否涵蓋 reasoning token：以真實 `kyt-main` 送 2 次（`max_tokens=256`，成本 < $0.01），回報的 `completion_tokens`（含 `reasoning_tokens`）不超過 256 | spec §7.3、§8.2 | 預留時另加 reasoning 的上限，或在白名單開放並強制 `reasoning_effort` |
| B8 | 輸入 token 的估計：請求 JSON 的字元數不小於 provider 回報的 `prompt_tokens`。取 kyt 真實請求（中文、英文與 hex 地址混合）送 3 次 Gemini | spec §8.2 | 估計乘上係數，係數取實測最大比值的 1.5 倍 |
| B9 | key 的 `budget_duration: "1mo"` 被 `/key/update` 接受，`budget_reset_at` 有值 | spec §6.3 | 改用 `"30d"` |
| B10 | pre-call 改寫的 `data["max_tokens"]` 會送到 provider，router fallback 也沿用 | spec §7.3 | 改為只拒絕、不補預設值 |
