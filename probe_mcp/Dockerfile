FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir "mcp==1.30.0"
COPY probe_mcp /app/probe_mcp
ENV PYTHONUNBUFFERED=1
EXPOSE 8100
CMD ["python", "-m", "probe_mcp.server"]
