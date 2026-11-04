# Optional local tracing

Tracing is disabled unless `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` is configured. An empty
endpoint initializes no OpenTelemetry SDK, exporter or tracing middleware. Logs remain
on stdout.

To use the disposable local Jaeger service, set these values in your private `.env`:

```dotenv
COMPOSE_PROFILES=traces
OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://jaeger:4318/v1/traces
```

Then start Compose with `docker compose up --build -d`. The local viewer is at
[localhost:16686](http://localhost:16686). `JAEGER_UI_PORT` and `JAEGER_OTLP_PORT` can
override host ports. Jaeger uses in-memory storage; restarting it discards traces.
For Python running directly on the host, use `http://127.0.0.1:4318/v1/traces` instead.

Only explicit internal gateway/browser calls propagate `traceparent`. External travel
providers receive no trace context or baggage. Finite span and attribute allowlists,
plus export filtering, exclude prompts, raw URLs and IDs, credentials, payloads,
exception text and stacks. Telemetry failure does not fail application requests.

Optional exporter headers belong only in environment variables. Never commit credentials
or expose them to the frontend. See [the helper reference](../one_request_telemetry/README.md)
for configuration and lifecycle details, and
[tracing tests](../backend/tests/test_tracing.py) for fixture privacy checks.
