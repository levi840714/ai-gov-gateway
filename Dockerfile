FROM docker.io/litellm/litellm:v1.105.1
# 映像預設沒有 pip：以 ensurepip 暫時安裝 Presidio，再移除 pip
# constraints 鎖住 Presidio 新增的相依；venv 既有的套件由 resolver 保留原版本
# 不含 spaCy 語言模型：AnalyzerEngine() 的預設 NLP engine 會在執行期下載 en_core_web 模型，程式只能直接使用 recognizer
COPY guardrails/presidio-constraints.txt /tmp/presidio-constraints.txt
RUN python -m ensurepip \
    && python -m pip install --no-cache-dir -c /tmp/presidio-constraints.txt "presidio-analyzer==2.2.364" \
    && python -m pip uninstall -y pip \
    && rm /tmp/presidio-constraints.txt
COPY src/ai_gov_gateway /opt/ai-gov-gateway/ai_gov_gateway
COPY guardrails /opt/ai-gov-gateway/guardrails
ENV PYTHONPATH=/opt/ai-gov-gateway
