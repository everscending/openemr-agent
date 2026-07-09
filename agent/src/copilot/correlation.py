"""Request-scoped correlation-ID plumbing.

Red-state baseline: the accessor exists but nothing ever sets the
contextvar yet (no middleware, no logging integration).
"""

from __future__ import annotations

from contextvars import ContextVar

_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def get_correlation_id() -> str | None:
    """Return the correlation ID for the current request, if any."""
    return _correlation_id.get()
