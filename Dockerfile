FROM docker.io/litellm/litellm:v1.105.1
# 映像內沒有 pip；執行期相依已在 LiteLLM 的 venv，直接以 PYTHONPATH 載入原始碼
COPY src/ai_gov_gateway /opt/ai-gov-gateway/ai_gov_gateway
ENV PYTHONPATH=/opt/ai-gov-gateway
