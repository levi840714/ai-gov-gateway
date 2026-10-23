FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir "starlette==0.47.2" "uvicorn==0.35.0"
COPY probe_llm /app/probe_llm
ENV PYTHONUNBUFFERED=1
EXPOSE 8300
CMD ["python", "-m", "probe_llm.server"]
