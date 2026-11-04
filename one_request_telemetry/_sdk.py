"""SDK construction reached only after the application's explicit endpoint gate."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import gzip
import logging
import queue
import random
import re
import threading
import time

from . import _attributes, _name


def build_runtime(settings, *, exporter=None, on_export=None):
    # All SDK/API/requests imports intentionally remain inside the enabled path.
    import requests
    from urllib3.util import Timeout
    from opentelemetry import context, trace
    from opentelemetry.metrics import NoOpMeterProvider
    from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import ReadableSpan, SpanLimits, TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
    from opentelemetry.sdk.util.instrumentation import InstrumentationScope
    from opentelemetry.trace import SpanContext, SpanKind, Status, StatusCode, TraceFlags, TraceState

    def failed(category):
        # Every caller supplies a code constant. No receiver object, reason,
        # exception, URL, header or acknowledgement text enters this logger.
        logging.getLogger("one_request_telemetry").warning("Tracing export failed: %s.", category)
        return SpanExportResult.FAILURE

    class PrivateSession(requests.Session):
        """No ambient proxy/netrc, redirects, URL-bearing exceptions or body logs."""

        def __init__(self):
            super().__init__()
            self.trust_env = False
            self.verify, self.cert, self.auth = True, None, None

        def request(self, *args, **kwargs):
            kwargs["allow_redirects"] = False
            # The exporter reads only a small success acknowledgement. Error
            # bodies never enter memory, SDK error objects or application logs.
            kwargs["stream"] = True
            try:
                return super().request(*args, **kwargs)
            except Exception:
                raise requests.ConnectionError("telemetry transport unavailable") from None

    class ExplicitOTLPExporter(SpanExporter):
        """Official protobuf encoding with an explicit, ambient-free HTTP client.

        OTLPSpanExporter's constructor also parses unrelated generic OTEL headers
        and resolves ambient certificate/credential providers. Supplying explicit
        settings does not disable that behavior in the pinned SDK. The small
        transport here intentionally never calls its configuration constructor.
        """

        def __init__(self, client):
            self.client = client
            self.stopped = threading.Event()
            self.headers = dict(settings.headers)
            self.headers.update({"Content-Type": "application/x-protobuf", "Content-Encoding": "gzip"})
            self.tasks = queue.Queue(maxsize=1)
            self.lock, self.busy = threading.Lock(), False
            self.worker = threading.Thread(target=self.run, name="one-request-tracing-transport", daemon=True)
            self.worker.start()

        def run(self):
            # At most one network operation per runtime, even if DNS or an HTTP
            # implementation ignores its timeout. Never spawn replacement
            # workers for a late/stalled request.
            while not self.stopped.is_set():
                try:
                    task = self.tasks.get(timeout=0.1)
                except queue.Empty:
                    continue
                try:
                    result = self.send(task.spans, task.deadline)
                    if time.monotonic() < task.deadline and not self.stopped.is_set():
                        task.result = result
                except Exception:
                    failed("transport_worker")
                finally:
                    with self.lock:
                        self.busy = False
                    task.done.set()
                    self.tasks.task_done()

        @staticmethod
        def retry_delay(value, fallback):
            try:
                if re.fullmatch(r"\d{1,8}", value):
                    return float(value)
                parsed = parsedate_to_datetime(value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
            except (ValueError, TypeError, OverflowError):
                return fallback

        def send(self, spans, deadline):
            if self.stopped.is_set():
                return SpanExportResult.FAILURE
            try:
                body = encode_spans(spans).SerializeToString()
                if len(body) > 1024 * 1024:
                    return failed("request_too_large")
                compressed = gzip.compress(body)
                for attempt in range(2):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or self.stopped.is_set():
                        break
                    retry, delay = False, random.uniform(0.8, 1.2)
                    response = None
                    try:
                        response = self.client.post(settings.endpoint, headers=self.headers, data=compressed,
                            timeout=Timeout(total=remaining, connect=min(1.0, remaining), read=remaining),
                            verify=True, cert=None, proxies={})
                        if response.status_code == 200:
                            acknowledgement = bytearray()
                            # Small acknowledgements must yield every byte:
                            # buffering a full chunk hides slow-drip progress
                            # from the whole-operation deadline check.
                            for chunk in response.iter_content(chunk_size=1):
                                acknowledgement.extend(chunk)
                                if len(acknowledgement) > 4096:
                                    return failed("ack_too_large")
                                if time.monotonic() >= deadline:
                                    return failed("deadline")
                            result = ExportTraceServiceResponse()
                            try:
                                result.ParseFromString(acknowledgement)
                            except Exception:
                                return failed("ack_invalid")
                            # Partial rejection must not invoke the successful
                            # evidence observer; never retry an acknowledgement.
                            return (failed("partial_rejection") if result.partial_success.rejected_spans
                                    else SpanExportResult.SUCCESS)
                        retry = response.status_code in {429, 502, 503, 504}
                        category = "http_rejected"
                        if retry:
                            delay = self.retry_delay(response.headers.get("Retry-After", ""), delay)
                    except requests.RequestException:
                        retry = True
                        category = "transport_error"
                    finally:
                        if response is not None:
                            response.close()
                    if (not retry or attempt == 1 or delay >= deadline - time.monotonic()
                            or self.stopped.wait(delay)):
                        return failed(category)
            except Exception:
                # Serialization, acknowledgement or transport failures never
                # enter SDK logging, which may include secret-bearing objects.
                return failed("encode_or_transport")
            return failed("deadline")

        def export(self, spans):
            # The SDK caller has an absolute bound independent of Requests'
            # per-I/O timeout, DNS, response framing or an uncooperative socket.
            # A late success does not become successful-export evidence.
            class ExportTask:
                def __init__(self):
                    self.spans, self.deadline = spans, time.monotonic() + 2.0
                    self.done, self.result = threading.Event(), SpanExportResult.FAILURE

            task = ExportTask()
            with self.lock:
                if self.stopped.is_set() or self.busy:
                    return failed("transport_busy_or_stopped")
                self.busy = True
                try:
                    self.tasks.put_nowait(task)
                except queue.Full:
                    self.busy = False
                    return SpanExportResult.FAILURE
            if task.done.wait(max(0.0, task.deadline - time.monotonic())) and time.monotonic() < task.deadline:
                return task.result
            return failed("deadline")

        def shutdown(self):
            self.stopped.set()
            self.client.close()

        def force_flush(self, timeout_millis=30000):
            return True

    resource = Resource({
        "service.name": settings.service_name,
        "deployment.environment.name": settings.environment,
        "service.version": settings.service_version,
    })
    known_routes: set[str] = set()
    routes_lock = threading.Lock()
    session = None
    if exporter is None:
        session = PrivateSession()
        try:
            exporter = ExplicitOTLPExporter(session)
        except BaseException:
            session.close()
            raise

    def clean_context(value):
        if value is None:
            return None
        return SpanContext(value.trace_id, value.span_id, value.is_remote,
            TraceFlags(int(value.trace_flags) & 1), TraceState())

    def clean_name(value, kind, attrs):
        if kind == SpanKind.SERVER:
            method = attrs.get("http.request.method", "_OTHER")
            route = attrs.get("http.route")
            return method + " " + route if route else "HTTP " + method
        return _name(value)

    class PrivacyExporter(SpanExporter):
        """Last boundary before any receiver: allowlist, no events/links/details."""

        def __init__(self, delegate):
            self.delegate, self.closed = delegate, False
            self.closing_deadline = None

        def export(self, spans):
            if self.closed or self.closing_deadline is not None and time.monotonic() >= self.closing_deadline:
                return SpanExportResult.FAILURE
            try:
                safe = []
                for value in spans:
                    attrs = _attributes(value.attributes)
                    # Only the matched application route observed by middleware
                    # may become http.route. Raw SDK callers cannot smuggle a
                    # path/token through this otherwise permitted attribute.
                    with routes_lock:
                        if attrs.get("http.route") not in known_routes:
                            attrs.pop("http.route", None)
                    safe.append(ReadableSpan(
                        name=clean_name(value.name, value.kind, attrs),
                        context=clean_context(value.context), parent=clean_context(value.parent),
                        resource=resource, attributes=attrs, events=(), links=(), kind=value.kind,
                        status=Status(value.status.status_code), start_time=value.start_time,
                        end_time=value.end_time,
                        instrumentation_scope=InstrumentationScope("one_request_telemetry", "1.0.0"),
                    ))
                safe = tuple(safe)
                result = self.delegate.export(safe)
                if result == SpanExportResult.SUCCESS and on_export is not None:
                    try:
                        on_export(safe)
                    except Exception:
                        logging.getLogger("one_request_telemetry").warning("Tracing observer failed.")
                return result
            except Exception:
                # In particular, never let BatchSpanProcessor log a delegate
                # exception containing credentials, payloads or receiver text.
                return failed("sanitize_or_delegate")

        def shutdown(self):
            self.closed = True
            try:
                self.delegate.shutdown()
            except Exception:
                pass
            finally:
                if session is not None:
                    session.close()

        def force_flush(self, timeout_millis=30000):
            try:
                return self.delegate.force_flush(timeout_millis)
            except Exception:
                return False

    guarded_exporter = PrivacyExporter(exporter)
    provider = None
    try:
        no_metrics = NoOpMeterProvider()
        provider = TracerProvider(resource=resource,
            sampler=ParentBased(TraceIdRatioBased(settings.sample_ratio)), shutdown_on_exit=False,
            meter_provider=no_metrics,
            span_limits=SpanLimits(max_attributes=24, max_events=0, max_links=0,
                max_span_attributes=24, max_event_attributes=0, max_link_attributes=0,
                max_attribute_length=180, max_span_attribute_length=180))
        provider.add_span_processor(BatchSpanProcessor(guarded_exporter,
            max_queue_size=256, max_export_batch_size=64, schedule_delay_millis=500,
            export_timeout_millis=2000, meter_provider=no_metrics))
        tracer = provider.get_tracer("one_request_telemetry", "1.0.0")
    except BaseException:
        if provider is not None:
            provider.shutdown()
        else:
            guarded_exporter.shutdown()
        raise

    class Runtime:
        def __init__(self):
            self.provider, self.tracer = provider, tracer
            self._closed = False

        def open_span(self, name, kind, attributes, *, context=None):
            kinds = {"internal": SpanKind.INTERNAL, "client": SpanKind.CLIENT,
                     "server": SpanKind.SERVER, "producer": SpanKind.PRODUCER,
                     "consumer": SpanKind.CONSUMER}
            return tracer.start_as_current_span(name, context=context,
                kind=kinds.get(kind, SpanKind.INTERNAL), attributes=attributes,
                record_exception=False, set_status_on_exception=False)

        def current_span(self):
            return trace.get_current_span()

        def attach_background(self, headers):
            pairs = [(key.encode('ascii'), value.encode('ascii')) for key, value in headers.items() if key == 'traceparent']
            return context.attach(self.request_context(pairs, True))

        def detach_background(self, token):
            context.detach(token)

        def allow_route(self, route):
            with routes_lock:
                known_routes.add(route)

        def mark_error(self, active, error):
            if isinstance(error, asyncio.CancelledError):
                active.set_attributes({"one_request.cancelled": True, "one_request.outcome": "cancelled"})
            else:
                safe_error = _attributes({"error_type": type(error).__name__}) or {"error.type": "Error"}
                active.set_attributes(safe_error)
                active.set_status(Status(StatusCode.ERROR))

        def mark_http_error(self, active):
            active.set_status(Status(StatusCode.ERROR))

        def request_context(self, headers, trust_incoming):
            fresh = context.Context()
            if not trust_incoming:
                return fresh
            values = [value for key, value in headers if key.lower() == b"traceparent"]
            if len(values) != 1 or len(values[0]) != 55:
                return fresh
            try:
                value = values[0].decode("ascii")
                # Accept only v00; no extension fields, baggage or tracestate.
                if not re.fullmatch(r"00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}", value):
                    return fresh
                trace_id, span_id, flags = int(value[3:35], 16), int(value[36:52], 16), int(value[53:55], 16)
                if not trace_id or not span_id:
                    return fresh
                parent = SpanContext(trace_id, span_id, True, TraceFlags(flags & 1), TraceState())
                return trace.set_span_in_context(trace.NonRecordingSpan(parent), fresh)
            except (ValueError, UnicodeError):
                return fresh

        def internal_headers(self):
            current = trace.get_current_span().get_span_context()
            if not current.is_valid:
                return {}
            return {"traceparent": f"00-{current.trace_id:032x}-{current.span_id:016x}-{int(current.trace_flags) & 1:02x}"}

        def force_flush(self, timeout_millis=3000):
            try:
                return provider.force_flush(timeout_millis=max(0, min(3000, int(timeout_millis))))
            except Exception:
                return False

        async def aclose(self):
            if self._closed:
                return
            self._closed = True
            # No default executor: a stuck third-party exporter must not make
            # asyncio.run wait for a worker thread during interpreter shutdown.
            # Drop queued shutdown batches after three seconds; a real HTTP
            # export already in flight has its own two-second deadline.
            guarded_exporter.closing_deadline = time.monotonic() + 3
            loop = asyncio.get_running_loop()
            finished = loop.create_future()

            def shutdown():
                try:
                    provider.shutdown()
                except Exception:
                    pass
                finally:
                    try:
                        loop.call_soon_threadsafe(complete)
                    except RuntimeError:
                        pass

            def complete():
                if not finished.done():
                    finished.set_result(None)

            threading.Thread(target=shutdown, name="one-request-tracing-shutdown", daemon=True).start()
            try:
                await asyncio.wait_for(asyncio.shield(finished), timeout=4.5)
            except TimeoutError:
                guarded_exporter.closed = True

    return Runtime()
