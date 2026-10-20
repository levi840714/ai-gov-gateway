FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir "mcp==1.30.0"
COPY account_ops /app/account_ops
ENV PYTHONUNBUFFERED=1
EXPOSE 8200
CMD ["python", "-m", "account_ops.server"]
