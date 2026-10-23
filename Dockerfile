FROM docker.io/litellm/litellm:v1.105.1
# 映像預設沒有 pip：以 ensurepip 暫時安裝 Presidio，再移除 pip
# pip 的 resolver 保留 venv 已有的版本，只補缺少的相依（spaCy 等，不含語言模型）
RUN python -m ensurepip \
    && python -m pip install --no-cache-dir "presidio-analyzer==2.2.364" \
    && python -m pip uninstall -y pip
COPY src/ai_gov_gateway /opt/ai-gov-gateway/ai_gov_gateway
COPY guardrails /opt/ai-gov-gateway/guardrails
ENV PYTHONPATH=/opt/ai-gov-gateway
