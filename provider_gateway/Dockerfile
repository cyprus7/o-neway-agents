FROM python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704 AS dependencies
WORKDIR /build
COPY backend/requirements*.txt ./backend/
RUN pip install --no-cache-dir --prefix=/install -r backend/requirements.txt

FROM python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704 AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PROVIDER_CACHE_PATH=/app/provider-data/cache.sqlite3
WORKDIR /app
COPY --from=dependencies /install /usr/local
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --create-home app && mkdir -p /app/provider-data && chown app:app /app/provider-data
COPY --chown=app:app backend/__init__.py ./backend/__init__.py
COPY --chown=app:app one_request_telemetry/ ./one_request_telemetry/
COPY --chown=app:app flight_search/__init__.py flight_search/models.py ./flight_search/
COPY --chown=app:app backend/app/__init__.py backend/app/data_gateway.py backend/app/models.py backend/app/providers.py backend/app/geography.py backend/app/retrieval.py ./backend/app/
COPY --chown=app:app backend/app/travel_dates.py ./backend/app/
COPY --chown=app:app backend/app/domain/ ./backend/app/domain/
COPY --chown=app:app backend/app/catalog/ ./backend/app/catalog/
COPY --chown=app:app backend/app/search/ ./backend/app/search/
COPY --chown=app:app backend/app/pricing/ ./backend/app/pricing/
COPY --chown=app:app backend/app/regional_events/ ./backend/app/regional_events/
COPY --chown=app:app backend/app/infrastructure/__init__.py backend/app/infrastructure/provider_cache.py ./backend/app/infrastructure/
USER app
EXPOSE 8091
HEALTHCHECK --interval=15s --timeout=3s --start-period=15s --retries=3 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8091/ready', timeout=2)"]
CMD ["uvicorn", "backend.app.data_gateway:app", "--host", "0.0.0.0", "--port", "8091", "--workers", "1"]
