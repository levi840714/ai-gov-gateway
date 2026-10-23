FROM docker.io/litellm/litellm:v1.105.1
# 映像沒有 pip：以 ensurepip 暫時安裝、裝完移除；constraints 鎖住 Presidio 新增的相依、不動既有套件
# 不裝 spaCy 語言模型（AnalyzerEngine 會在執行期下載），程式只直接使用個別 recognizer
COPY guardrails/presidio-constraints.txt /tmp/presidio-constraints.txt
RUN python -m ensurepip \
    && python -m pip install --no-cache-dir -c /tmp/presidio-constraints.txt "presidio-analyzer==2.2.364" \
    && python -m pip uninstall -y pip \
    && rm /tmp/presidio-constraints.txt
COPY src/ai_gov_gateway /opt/ai-gov-gateway/ai_gov_gateway
COPY guardrails /opt/ai-gov-gateway/guardrails
ENV PYTHONPATH=/opt/ai-gov-gateway
