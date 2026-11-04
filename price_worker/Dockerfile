FROM mcr.microsoft.com/playwright/python:v1.63.0-noble@sha256:72bd171a9ffc2b4b59532aaa6210e21014d07093120dc25528870c0b840da1f0
WORKDIR /app
COPY price_worker/requirements.txt ./price_worker/requirements.txt
COPY backend/requirements-tracing.txt ./backend/requirements-tracing.txt
RUN pip install --no-cache-dir -r price_worker/requirements.txt && groupadd --gid 10001 crawler && useradd --uid 10001 --gid 10001 --create-home crawler
COPY --chown=crawler:crawler price_worker/ ./price_worker/
COPY --chown=crawler:crawler one_request_telemetry/ ./one_request_telemetry/
USER 10001:10001
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
CMD ["uvicorn", "price_worker.app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
