"""Observability surface for the agent service (T014).

ARCHITECTURE.md §7: the service emits OpenTelemetry spans; agent code never
imports a vendor tracing SDK; service logs and spans carry pointers (ids,
counts, class names), never PHI content.

Package layout, by import discipline:

- :mod:`copilot.telemetry.tracing` — the vendor-neutral ``opentelemetry.trace``
  API surface (span-name constants, the correlation-id/error helpers). Safe
  to import from anywhere.
- :mod:`copilot.telemetry.pricing` — pure ``Decimal`` cost computation from a
  configurable price table. No OTel dependency at all.
- :mod:`copilot.telemetry.metrics` — process-wide counters for the
  ``/metrics`` endpoint. No OTel dependency at all.
- :mod:`copilot.telemetry.import_guard` — the AST-based portability guard
  (criterion 4). No OTel dependency at all.
- :mod:`copilot.telemetry.bootstrap` — the **one** module permitted to import
  ``opentelemetry.sdk`` / ``opentelemetry.exporter.*``, wired lazily from
  :func:`copilot.app.create_app`.
"""

from __future__ import annotations
