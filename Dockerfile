FROM node:22-alpine@sha256:0a7108bf6c7bf5de370ffb1a3ed6be93d405b43ff159f681a8d18c0e2bc2e402 AS web
WORKDIR /build/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
COPY design/ /build/design/
RUN npm run build

FROM python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704 AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/app/data FRONTEND_DIST=/app/frontend/dist
WORKDIR /app
COPY backend/requirements-agent.txt backend/requirements-tracing.txt ./backend/
RUN pip install --no-cache-dir -r backend/requirements-agent.txt && groupadd --gid 10001 app && useradd --uid 10001 --gid app --create-home app && mkdir -p /app/data && chown app:app /app/data
COPY --chown=app:app backend/__init__.py ./backend/__init__.py
COPY --chown=app:app one_request_telemetry/ ./one_request_telemetry/
COPY --chown=app:app backend/app/__init__.py backend/app/main.py backend/app/planner.py backend/app/models.py backend/app/llm.py backend/app/storage.py backend/app/checkout.py backend/app/geography.py ./backend/app/
COPY --chown=app:app backend/app/travel_dates.py ./backend/app/
COPY --chown=app:app backend/app/application/ ./backend/app/application/
COPY --chown=app:app backend/app/domain/ ./backend/app/domain/
COPY --chown=app:app backend/app/transport/ ./backend/app/transport/
COPY --chown=app:app backend/app/migrations/ ./backend/app/migrations/
COPY --chown=app:app backend/app/infrastructure/__init__.py backend/app/infrastructure/http_travel_data.py backend/app/infrastructure/knowledge_travel_data.py backend/app/infrastructure/session_notifications.py backend/app/infrastructure/command_worker.py backend/app/infrastructure/telegram_client.py backend/app/infrastructure/reminders.py ./backend/app/infrastructure/
COPY --chown=app:app backend/app/pricing/__init__.py backend/app/pricing/models.py backend/app/pricing/aggregation.py backend/app/pricing/normalization.py backend/app/pricing/demo.py backend/app/pricing/demo_models.py ./backend/app/pricing/
COPY --chown=app:app backend/app/regional_events/__init__.py backend/app/regional_events/domain.py ./backend/app/regional_events/
COPY --chown=app:app scripts/ ./scripts/
COPY --from=web --chown=app:app /build/frontend/dist ./frontend/dist
USER app
# Fail the build when the selectively packaged runtime cannot load its entry point.
# These switches apply only to this check; no provider credentials or requests are needed.
RUN LLM_ENABLED=0 WEB_FALLBACK_ENABLED=0 TELEGRAM_ENABLED=0 python -c "from backend.app.main import app; assert app is not None"
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --start-period=15s --retries=3 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2)"]
CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
