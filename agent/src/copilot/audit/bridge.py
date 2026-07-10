"""The audit-bridge HTTP client — fail-open-with-alarm (T013).

ARCHITECTURE.md:367 — "Fail-closed applies to verification; audit delivery
is fail-open-with-alarm." This is the one seam in the service where a failed
network call must never affect the user-facing response: :meth:`deliver`
always returns normally, regardless of what the bridge does. A non-2xx
status, a timeout, and a refused connection are handled identically:
increment the failure counter, emit one structured alert, return.

The bridge POST is bounded (default 2s, ``DEFAULT_AUDIT_TIMEOUT_SECONDS``):
``asyncio.timeout`` cancels a hung call at the deadline rather than awaiting
it to completion, mirroring ``copilot.fhir.client.FhirClient``'s pattern.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

import httpx

from copilot.audit.record import AuditInvocationRecord

#: ARCHITECTURE.md §7: the audit POST must not extend user-facing latency —
#: bounded well under any request-level budget.
DEFAULT_AUDIT_TIMEOUT_SECONDS: float = 2.0

#: Stable logger name so callers (and tests) don't need to guess this
#: module's dotted path — mirrors ``CORRELATION_ID_HEADER`` being an exported
#: constant rather than a string callers reconstruct themselves.
AUDIT_BRIDGE_LOGGER_NAME = "copilot.audit.bridge"

#: The one alert this client ever emits on a failed delivery attempt.
AUDIT_BRIDGE_DELIVERY_FAILED_EVENT = "audit_bridge_delivery_failed"


class AuditMetricsRecorder(Protocol):
    """Counts audit-delivery attempts — exactly one increment per attempt.

    T014 exports a concrete implementation on the observability surface;
    here it is only the injection seam, so delivery outcomes are countable
    without pulling a metrics backend into this ticket's scope.
    """

    def record_success(self) -> None: ...

    def record_failure(self) -> None: ...


class NullAuditMetricsRecorder:
    """No-op recorder — the default when no counter backend is wired yet."""

    def record_success(self) -> None:
        return None

    def record_failure(self) -> None:
        return None


class AuditBridgeClient:
    """POSTs :class:`AuditInvocationRecord`\\ s to OpenEMR's audit-bridge endpoint.

    ``base_url`` is the full, already-configured endpoint URL — this client
    performs a single POST to it, no path joining. ``transport`` is the
    ``httpx.AsyncBaseTransport`` seam tests substitute (a mock transport that
    5xxs, hangs past ``timeout``, or raises a connection error).
    """

    def __init__(
        self,
        *,
        base_url: str,
        metrics: AuditMetricsRecorder | None = None,
        timeout: float = DEFAULT_AUDIT_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._url = base_url
        self._timeout = timeout
        self._metrics: AuditMetricsRecorder = (
            metrics if metrics is not None else NullAuditMetricsRecorder()
        )
        self._logger = logger if logger is not None else logging.getLogger(
            AUDIT_BRIDGE_LOGGER_NAME
        )
        self._client = httpx.AsyncClient(
            transport=transport, timeout=httpx.Timeout(timeout)
        )

    async def aclose(self) -> None:
        """Release the underlying HTTP connection pool."""
        await self._client.aclose()

    async def deliver(self, record: AuditInvocationRecord) -> None:
        """Attempt one delivery. Never raises; never blocks past ``timeout``.

        Success (2xx) increments the success counter. Every other outcome —
        non-2xx, timeout, connection refused, or any other transport error —
        increments the failure counter and emits one
        ``audit_bridge_delivery_failed`` alert carrying the correlation ID
        and either the HTTP status or the exception's *type name* — never
        the record body, an exception message, or response text.
        """
        try:
            async with asyncio.timeout(self._timeout):
                response = await self._client.post(
                    self._url,
                    content=record.model_dump_json(),
                    headers={"Content-Type": "application/json"},
                )
        except (TimeoutError, httpx.TimeoutException) as exc:
            self._fail(record.correlation_id, exception=exc)
            return
        except httpx.HTTPError as exc:
            self._fail(record.correlation_id, exception=exc)
            return

        if 200 <= response.status_code < 300:
            self._metrics.record_success()
            return
        self._fail(record.correlation_id, status_code=response.status_code)

    def _fail(
        self,
        correlation_id: str,
        *,
        status_code: int | None = None,
        exception: BaseException | None = None,
    ) -> None:
        self._metrics.record_failure()
        # Built via `makeRecord` + direct attribute assignment, not the
        # `logger.error(..., extra={...})` path: the process may already have
        # a correlation-aware LogRecord factory installed (this service's own
        # ``install_correlation_log_record_factory``, T001) that stamps a
        # ``correlation_id`` attribute on every record from the ambient
        # request context. Going through `extra` would collide with that
        # (``logging`` raises ``KeyError`` on an attribute `extra` re-sets).
        # Setting the attribute directly afterward always wins with *this*
        # call's own correlation ID, regardless of what ambient state exists.
        record = self._logger.makeRecord(
            self._logger.name,
            logging.ERROR,
            __file__,
            0,
            AUDIT_BRIDGE_DELIVERY_FAILED_EVENT,
            (),
            None,
        )
        record.correlation_id = correlation_id
        record.status_code = status_code
        record.exception_type = (
            type(exception).__name__ if exception is not None else None
        )
        self._logger.handle(record)
