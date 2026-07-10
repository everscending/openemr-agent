"""Audit-bridge invocation records and client (T013).

ARCHITECTURE.md §7: "Fail-closed applies to verification; audit delivery is
fail-open-with-alarm." See :mod:`copilot.audit.bridge` for the client and
:mod:`copilot.audit.record` for the PHI-free record contract.
"""

from copilot.audit.bridge import (
    AUDIT_BRIDGE_DELIVERY_FAILED_EVENT,
    AUDIT_BRIDGE_LOGGER_NAME,
    DEFAULT_AUDIT_TIMEOUT_SECONDS,
    AuditBridgeClient,
    AuditMetricsRecorder,
    NullAuditMetricsRecorder,
)
from copilot.audit.record import AuditInvocationRecord, AuditOutcome

__all__ = [
    "AUDIT_BRIDGE_DELIVERY_FAILED_EVENT",
    "AUDIT_BRIDGE_LOGGER_NAME",
    "DEFAULT_AUDIT_TIMEOUT_SECONDS",
    "AuditBridgeClient",
    "AuditInvocationRecord",
    "AuditMetricsRecorder",
    "AuditOutcome",
    "NullAuditMetricsRecorder",
]
