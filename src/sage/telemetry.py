"""Optional OpenTelemetry tracing for the self-hosted SAGE service."""

from dataclasses import dataclass
from typing import Any

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.trace import NoOpTracerProvider, Tracer


@dataclass(slots=True)
class TelemetryRuntime:
    tracer: Tracer
    provider: TracerProvider | None = None

    def shutdown(self) -> None:
        if self.provider is not None:
            self.provider.shutdown()


def configure_telemetry(
    *,
    service_name: str,
    endpoint: str | None,
    insecure: bool,
    span_exporter: Any | None = None,
    batch: bool = True,
) -> TelemetryRuntime:
    if endpoint is None and span_exporter is None:
        provider = NoOpTracerProvider()
        return TelemetryRuntime(provider.get_tracer(service_name))
    exporter = span_exporter or OTLPSpanExporter(endpoint=endpoint, insecure=insecure)
    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": service_name,
                "service.version": "0.1.0",
            }
        )
    )
    processor = BatchSpanProcessor(exporter) if batch else SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    return TelemetryRuntime(provider.get_tracer(service_name), provider)


def record_http_result(span: trace.Span, *, route: str, status_code: int) -> None:
    span.update_name(route)
    span.set_attribute("http.route", route)
    span.set_attribute("http.response.status_code", status_code)
    if status_code >= 500:
        span.set_status(trace.Status(trace.StatusCode.ERROR))