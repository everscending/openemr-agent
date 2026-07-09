"""Request-scoped correlation-ID plumbing.

Every HTTP request is assigned a correlation ID: either the value the
client supplied in the ``X-Correlation-ID`` request header, or a freshly
generated UUIDv4. The ID is echoed back on the response, exposed to
handler code via :func:`get_correlation_id`, and injected into every
:class:`logging.LogRecord` emitted while the request is in flight.
"""

from __future__ import annotations

import logging
import uuid
from contextvars import ContextVar
from typing import Any

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CORRELATION_ID_HEADER = "X-Correlation-ID"

_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)

_record_factory_installed: bool = False


def get_correlation_id() -> str | None:
    """Return the correlation ID for the current request, if any."""
    return _correlation_id.get()


class CorrelationIdMiddleware:
    """Pure ASGI middleware managing the per-request correlation ID.

    Echoes a client-supplied ``X-Correlation-ID`` header verbatim,
    otherwise generates a UUIDv4. The value is stored in a contextvar for
    the duration of the request and stamped onto the response headers.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        supplied = Headers(scope=scope).get(CORRELATION_ID_HEADER)
        correlation_id = supplied if supplied else str(uuid.uuid4())
        token = _correlation_id.set(correlation_id)

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[CORRELATION_ID_HEADER] = correlation_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        finally:
            _correlation_id.reset(token)


def install_correlation_log_record_factory() -> None:
    """Make every ``LogRecord`` carry a ``correlation_id`` attribute.

    Wraps the active log-record factory so any record emitted anywhere in
    the process — regardless of which logger it came from — gets the
    current request's correlation ID as a structured field (``None``
    outside a request). Idempotent: safe to call once per ``create_app``.
    """
    global _record_factory_installed
    if _record_factory_installed:
        return

    previous_factory = logging.getLogRecordFactory()

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous_factory(*args, **kwargs)
        record.correlation_id = get_correlation_id()
        return record

    logging.setLogRecordFactory(factory)
    _record_factory_installed = True
