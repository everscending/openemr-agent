"""The one module permitted to import the OpenTelemetry SDK/exporters
(T014, exporter/service-name env wiring completed by T028).

ARCHITECTURE.md:422 — agent code never imports a vendor tracing SDK; only
this bootstrap module does, wired lazily (never at another module's top
level — see ``copilot.telemetry.import_guard``'s "Scope" note) from
``copilot.app.create_app``. Everywhere else, only the vendor-neutral
``opentelemetry.trace`` API is used (:mod:`copilot.telemetry.tracing`). The
exporter destination — endpoint, headers, and resource ``service.name`` — is
entirely environment configuration, never code (ARCHITECTURE.md §7's
portability rule (a)/(c); T028 criterion 1) — LangSmith or any other backend
is an OTel exporter destination, configured via
``OTEL_EXPORTER_OTLP_ENDPOINT`` (+ ``OTEL_EXPORTER_OTLP_HEADERS``, read by
``OTLPSpanExporter`` itself) and ``OTEL_SERVICE_NAME``, not coded against.
Account wiring, dashboards, and alert definitions live under
``docs/observability/`` (T028), never here.

Tests never exercise this module's exporter path — they build their own
``TracerProvider`` + ``InMemorySpanExporter`` and inject the resulting
``Tracer`` directly into ``create_app(tracer=...)``, bypassing this module
entirely (see ``copilot.telemetry.tracing.get_tracer``'s docstring). This
keeps every test independent of process-global tracer-provider state.
"""

from __future__ import annotations

import logging
import os

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter

#: Standard OTel environment variable naming the collector endpoint. Unset,
#: no exporter is wired — spans are still created (against a real, inert
#: ``TracerProvider``) but never sent anywhere; this mirrors the audit
#: bridge's "fail open on missing configuration" posture (ARCHITECTURE.md
#: §7): tracing is opt-in via deploy-time config, never a startup failure.
OTEL_EXPORTER_OTLP_ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_ENDPOINT"

#: Standard OTel environment variable for the resource's ``service.name``
#: (T028 criterion 1: endpoint, headers, *and* service name are all
#: env-only — none hardcoded past a sane default). Headers need no constant
#: here: ``OTLPSpanExporter`` reads ``OTEL_EXPORTER_OTLP_HEADERS`` /
#: ``OTEL_EXPORTER_OTLP_TRACES_HEADERS`` itself whenever this module leaves
#: ``headers=`` unset, so there is nothing for this module to read or thread
#: through.
OTEL_SERVICE_NAME_ENV = "OTEL_SERVICE_NAME"

_DEFAULT_SERVICE_NAME = "copilot"

_logger = logging.getLogger(__name__)
_configured = False


def _otlp_exporter_from_env() -> SpanExporter | None:
    endpoint = os.environ.get(OTEL_EXPORTER_OTLP_ENDPOINT_ENV)
    if not endpoint:
        return None
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
    except ImportError:
        _logger.warning(
            "otlp_exporter_package_unavailable",
            extra={"endpoint_configured": True},
        )
        return None
    return OTLPSpanExporter(endpoint=endpoint)


def configure_tracing(*, exporter: SpanExporter | None = None) -> trace.Tracer:
    """Install a global ``TracerProvider`` (once) and return this service's tracer.

    ``exporter`` is an injection seam for callers that already built one;
    omitted, the exporter is resolved from ``OTEL_EXPORTER_OTLP_ENDPOINT`` —
    unset, no exporter is attached (spans are created and immediately
    dropped, never a crash, never a network call). Idempotent: only the
    first call installs the process-wide provider (mirrors
    ``install_correlation_log_record_factory``'s pattern in
    :mod:`copilot.correlation`) — later calls just return this service's
    tracer against whatever provider is already active.
    """
    global _configured
    if not _configured:
        resolved_exporter = (
            exporter if exporter is not None else _otlp_exporter_from_env()
        )
        service_name = os.environ.get(OTEL_SERVICE_NAME_ENV, _DEFAULT_SERVICE_NAME)
        provider = TracerProvider(
            resource=Resource.create({"service.name": service_name})
        )
        if resolved_exporter is not None:
            provider.add_span_processor(SimpleSpanProcessor(resolved_exporter))
        trace.set_tracer_provider(provider)
        _configured = True
    # The tracer *instrumentation-scope* name is distinct from the resource's
    # ``service.name`` above (which is env-configurable) — this one matches
    # :mod:`copilot.telemetry.tracing`'s ``_TRACER_NAME`` and stays fixed.
    return trace.get_tracer(_DEFAULT_SERVICE_NAME)
