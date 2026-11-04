# Optional tracing helper

The package imports no OpenTelemetry module until an explicitly configured app
starts its lifespan. Call `instrument_app(app, "one-request-agent")` after creating
the FastAPI app. It returns the same app and preserves the existing lifespan.
Private gateway/worker apps use `trust_incoming=True`; the public agent uses the
default and starts independent roots.

`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` is the only enablement switch. It must be a
full HTTP(S) URL ending in `/v1/traces`, without credentials/query/fragment.
`OTEL_EXPORTER_OTLP_TRACES_HEADERS` accepts comma-separated `key=value` headers
(percent-encoded values are supported). Neither setting is included in repr or
logs. Metadata is explicit: `OTEL_DEPLOYMENT_ENVIRONMENT` (default `local`),
`OTEL_SERVICE_VERSION` (default `0.1.0`) and `OTEL_TRACES_SAMPLER_ARG` (finite 0..1,
default 1). No global provider, instrumentation, host/process/environment resource
detectors, logs exporter, or metrics exporter is installed.

Use `@traced("planning.run")`, `with span("catalog.lookup")`,
`set_attributes(operation="inventory", outcome="ok")` and
`async with queued(semaphore_or_lock, "gateway.queue")`. Span names and string
attributes must be constants from the package's finite allowlists, never
user/provider text. New owned operations require extending `_SPAN_NAMES` and any
new controlled string values require extending `_STRING_VALUES` in the helper.
Allowed aliases are operation, outcome, provider, attempt, count, cache_hit,
wait_seconds, error_type, cancelled, http_method, http_route, http_status_code and
http_response_status_code. Canonical dotted attribute names are also accepted.
All other attributes/string values/span names, all events/links, status descriptions, resource detectors
and trace state are discarded again at export. Exceptions record class only.

`internal_headers()` returns only `traceparent`. Call it only for the two owned
private HTTP boundaries, never on general shared HTTP clients. Probe/static/SSE
routes are omitted. Middleware binds the app runtime per request; background
tasks inherit it through Python context variables.

For tests, pass a factory to `instrument_app`:

```python
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from one_request_telemetry import create_runtime, instrument_app

exporter = InMemorySpanExporter()
created = []

def runtime_factory(settings):
    runtime = create_runtime(settings, exporter=exporter)
    created.append(runtime)
    return runtime

instrument_app(app, "one-request-test", runtime_factory=runtime_factory)
```

The endpoint gate still applies to injected factories. Runtime factories receive
`TracingSettings(endpoint, headers, service_name, environment, service_version,
sample_ratio)`; the last three default to `local`, `0.1.0`, and `1.0`. During the
lifespan, `app.state.tracing` holds the runtime; it is cleared during shutdown.
`runtime.force_flush(timeout_millis=3000)` is a synchronous test/evidence helper,
not a request operation. `await runtime.aclose()` closes owned SDK resources off
the event loop and returns within 4.5 seconds even for an uncooperative exporter.
The background batch queue is 256 spans, batch size 64, delay 500 ms, and the HTTP
export timeout 2 seconds. Export failure drops telemetry without raising into
requests. The HTTP session ignores ambient proxies/netrc, refuses redirects, and
does not expose receiver bodies or secret-bearing exceptions to SDK logging.
The explicit HTTP exporter uses the official OpenTelemetry protobuf encoder;
it avoids the SDK exporter's ambient configuration parser, which otherwise merges
generic OTEL headers/certificate settings even when explicit settings are passed.
Only the validated trace endpoint/headers above apply: unrelated OTEL endpoints,
headers, compression, timeouts, credential providers and certificate paths are
never inherited. No process environment is modified. Requests are capped at
1 MiB, successful acknowledgements at 4 KiB, and at most two attempts share the
two-second export budget. Only retryable OTLP HTTP statuses/connection failures
retry; receiver reasons, bodies and acknowledgement text are never logged.
Each enabled runtime has one daemon HTTP worker and a single bounded work slot.
The SDK export call returns after at most two seconds even if DNS or a transport
ignores its I/O timeout. Further batches drop while that worker is still busy;
no additional transport workers are spawned, and late success cannot invoke the
evidence observer. Shutdown signals/closes the transport without waiting for an
uncooperative worker; at most that one daemon thread can remain until I/O returns.

For synthetic evidence, `create_runtime(settings, on_export=callback)` retains
the real guarded OTLP exporter and calls the observer with a tuple of sanitized
`ReadableSpan` objects only after a successful OTLP export. Observer exceptions
are ignored. The callback executes on the exporter thread and must be quick; it
receives no endpoint, headers, unfiltered data, or response body.
