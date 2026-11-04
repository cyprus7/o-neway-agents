FROM mcr.microsoft.com/playwright/python:v1.63.0-noble@sha256:72bd171a9ffc2b4b59532aaa6210e21014d07093120dc25528870c0b840da1f0 AS dependencies
WORKDIR /build
COPY flight_search/requirements.txt ./flight_search/requirements.txt
COPY backend/requirements-tracing.txt ./backend/requirements-tracing.txt
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r flight_search/requirements.txt

FROM mcr.microsoft.com/playwright/python:v1.63.0-noble@sha256:72bd171a9ffc2b4b59532aaa6210e21014d07093120dc25528870c0b840da1f0 AS runtime
WORKDIR /app
COPY --from=dependencies /wheels /wheels
COPY flight_search/requirements.txt ./flight_search/requirements.txt
COPY backend/requirements-tracing.txt ./backend/requirements-tracing.txt
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r flight_search/requirements.txt && rm -rf /wheels && groupadd --gid 10001 crawler && useradd --uid 10001 --gid 10001 --create-home crawler
COPY --chown=crawler:crawler flight_search/ ./flight_search/
COPY --chown=crawler:crawler one_request_telemetry/ ./one_request_telemetry/
USER 10001:10001
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["uvicorn", "flight_search.app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
