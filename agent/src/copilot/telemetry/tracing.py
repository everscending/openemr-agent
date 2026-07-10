"""Vendor-neutral OTel API surface used throughout instrumented code (T014).

Only ``opentelemetry.trace`` (the API) is imported here — never the SDK, never
any exporter (ARCHITECTURE.md:422). This is the module every other
instrumented file (the agent loop, the FastAPI app) imports for tracer
access, span-name constants, and the shared error-recording helper. The SDK
and any exporter are wired only by :mod:`copilot.telemetry.bootstrap`.

Span names are static, low-cardinality constants — never interpolate a
patient id, a conversation id, a question, or a tool argument into a span
name. Identifiers belong in attributes.
"""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode, Tracer

#: Root span for one ``/chat`` request.
SPAN_CHAT_REQUEST = "chat.request"
#: Child span for one LLM turn.
SPAN_LLM_CALL = "llm.call"
#: Child span for one tool execution.
SPAN_TOOL_CALL = "tool.call"
#: Child span for the T008/T009 verification pass over one draft.
SPAN_VERIFY_RESPONSE = "verify.response"

#: Attribute key stamped on every span this service creates.
CORRELATION_ID_ATTR = "correlation_id"
#: Attribute key for the exception *class name* only — never a message.
ERROR_TYPE_ATTR = "error.type"

_TRACER_NAME = "copilot"


def get_tracer() -> Tracer:
    """This service's tracer, against whatever provider is globally active.

    Production: configured once via :mod:`copilot.telemetry.bootstrap`, wired
    from ``create_app``. Tests inject their own :class:`Tracer` (built from a
    private ``TracerProvider`` + ``InMemorySpanExporter``) directly into the
    constructors that accept one, instead of relying on global state — so
    exported spans never leak between tests.
    """
    return trace.get_tracer(_TRACER_NAME)


def mark_error(span: Span, exc: BaseException) -> None:
    """Record a failure on ``span`` without leaking the exception message.

    Sets ``error.type`` to the exception's class name only and marks the span
    status as an error with **no description** — never
    ``span.record_exception(exc)``, which would write the message/traceback
    into a span event. This codebase's exception messages carry FHIR URLs and
    bridge payloads (ARCHITECTURE.md §7); the same rule T013 applied to its
    alert applies here, for the same reason.
    """
    span.set_attribute(ERROR_TYPE_ATTR, type(exc).__name__)
    span.set_status(Status(StatusCode.ERROR))
