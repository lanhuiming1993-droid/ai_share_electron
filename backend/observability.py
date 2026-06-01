from __future__ import annotations

import os
from urllib.parse import urlsplit

from backend.logging_config import get_logger, log_event, log_exception

logger = get_logger("observability")


def configure_optional_telemetry(app) -> dict:
    endpoint = os.environ.get("ALPHADESK_OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        return {
            "status": "disabled",
            "detail": "Set ALPHADESK_OTEL_EXPORTER_OTLP_ENDPOINT to enable optional OTLP tracing.",
        }
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.requests import RequestsInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(resource=Resource.create({"service.name": "alphadesk-local-api"}))
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
        trace.set_tracer_provider(provider)
        FastAPIInstrumentor.instrument_app(app, excluded_urls="health.*")
        RequestsInstrumentor().instrument()
    except ImportError as exc:
        log_exception(logger, "telemetry.dependencies_missing", exc)
        return {
            "status": "missing_dependencies",
            "detail": "Install requirements-observability.txt before enabling OTLP tracing.",
        }
    except Exception as exc:
        log_exception(logger, "telemetry.configuration_failed", exc)
        return {"status": "configuration_failed", "detail": f"{type(exc).__name__}: inspect local diagnostics for details."}
    parsed_endpoint = urlsplit(endpoint)
    collector = parsed_endpoint.hostname or "configured"
    try:
        port = parsed_endpoint.port
    except ValueError:
        port = None
    if port:
        collector = f"{collector}:{port}"
    log_event(logger, "INFO", "telemetry.enabled", collector=collector)
    return {"status": "enabled", "collector": collector}
