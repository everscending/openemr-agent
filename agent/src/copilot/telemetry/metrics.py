"""Process-wide counters exposed via ``/metrics`` (T014 criterion 6).

Counters only. No patient id, conversation id, correlation id, or bearer
token ever crosses this surface — a metrics scrape is not request-scoped and
must never become a PHI channel (ARCHITECTURE.md §7).

Pure module: no OpenTelemetry import of any kind.
"""

from __future__ import annotations

from threading import Lock
from typing import Protocol


class ToolFailureRecorder(Protocol):
    """The agent loop's injection seam for counting tool execution failures."""

    def record_tool_failure(self) -> None: ...


class NullToolFailureRecorder:
    """Default no-op — tests/callers wire a real counter explicitly."""

    def record_tool_failure(self) -> None:
        return None


class TelemetryMetrics:
    """Thread-safe in-process counters: audit delivery outcomes + tool failures.

    Implements both :class:`~copilot.audit.bridge.AuditMetricsRecorder` (so an
    instance can be handed directly to ``AuditBridgeClient``) and
    :class:`ToolFailureRecorder`, and exposes a plain-``int`` snapshot for the
    ``/metrics`` endpoint.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._audit_success = 0
        self._audit_failure = 0
        self._tool_failure = 0

    def record_success(self) -> None:
        with self._lock:
            self._audit_success += 1

    def record_failure(self) -> None:
        with self._lock:
            self._audit_failure += 1

    def record_tool_failure(self) -> None:
        with self._lock:
            self._tool_failure += 1

    def snapshot(self) -> dict[str, int]:
        """Counters only — never request-scoped data (criterion 6)."""
        with self._lock:
            return {
                "audit_delivery_success_total": self._audit_success,
                "audit_delivery_failure_total": self._audit_failure,
                "tool_failure_total": self._tool_failure,
            }
