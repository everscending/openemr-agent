"""Readiness probing for the Co-Pilot service (T004).

The ``/ready`` endpoint validates three meaningful dependencies —
``openemr``, ``llm_provider``, and ``trace_backend`` — via injectable
async checker functions. This module owns concurrency, the overall
deadline, per-probe latency measurement, and response shaping; checkers
only perform a lightweight reachability call (or raise on failure).

T045: the ``openemr`` checker (this key was previously named after the
FHIR dependency it originally probed) probes OpenEMR's site root with a
plain, unauthenticated GET — not the FHIR ``/metadata`` endpoint.
``/metadata`` sits behind the same auth gate as the rest of the FHIR API
and 503s in production regardless of OpenEMR's actual health, per
PRD.md:325-329 ("generic OpenEMR reachability, no FHIR mention"). The
root URL is derived from ``OPENEMR_FHIR_BASE_URL`` (same host in every
environment) — no new env var.

Per ARCHITECTURE.md section 7 portability rule (b), the trace backend is
reported under the generic ``trace_backend`` key — a configured
dependency, never a named vendor product.

T028: ``trace_backend``'s checker is sourced from
``OTEL_EXPORTER_OTLP_ENDPOINT`` — the *same* env var
:mod:`copilot.telemetry.bootstrap` reads to decide whether to wire a real
OTLP exporter — so this endpoint can never disagree with what actually gets
exported. The literal env-var name is duplicated here rather than imported
from ``bootstrap`` on purpose: importing that module at module level would
pull the OTel SDK/exporter into this module's transitive closure and trip
the T014 import guard (only ``copilot.telemetry.bootstrap`` itself is
exempt, and only for its own direct imports — see
``copilot.telemetry.import_guard``'s docstring).
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import urlsplit

import httpx

Checker = Callable[[], Awaitable[None]]
"""Async callable that returns on success and raises on failure."""

DEPENDENCY_KEYS: tuple[str, ...] = ("openemr", "llm_provider", "trace_backend")

DEFAULT_DEADLINE_SECONDS = 5.0
PROBE_HTTP_TIMEOUT_SECONDS = 4.0

OPENEMR_FHIR_BASE_URL_ENV = "OPENEMR_FHIR_BASE_URL"
LLM_PROVIDER_URL_ENV = "LLM_PROVIDER_URL"
#: T028: the trace backend's readiness probe reads the *real* exporter
#: config var — see the module docstring's "T028" note for why this is a
#: duplicated literal rather than an import of
#: ``copilot.telemetry.bootstrap.OTEL_EXPORTER_OTLP_ENDPOINT_ENV``.
OTEL_EXPORTER_OTLP_ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_ENDPOINT"
READINESS_DEADLINE_ENV = "READINESS_DEADLINE_SECONDS"


async def run_readiness_checks(
    checkers: Mapping[str, Checker],
    *,
    deadline: float = DEFAULT_DEADLINE_SECONDS,
) -> tuple[int, dict[str, dict[str, Any]]]:
    """Probe all dependencies concurrently under an overall deadline.

    Returns ``(http_status, body)`` where ``body`` maps each dependency key
    to ``{"status": "ok", "latency_ms": float}`` on success or
    ``{"status": "error", "latency_ms": float, "error": str}`` on failure.
    A probe that exceeds ``deadline`` is reported as a timeout failure for
    that dependency; the endpoint itself never hangs on it.
    """
    keys = list(checkers.keys())
    results = await asyncio.gather(
        *(_run_probe(checkers[key], deadline=deadline) for key in keys)
    )
    body = dict(zip(keys, results))
    all_ok = all(entry["status"] == "ok" for entry in body.values())
    return (200 if all_ok else 503), body


async def _run_probe(checker: Checker, *, deadline: float) -> dict[str, Any]:
    start = time.perf_counter()
    try:
        await asyncio.wait_for(checker(), timeout=deadline)
    except (TimeoutError, asyncio.TimeoutError):
        return {
            "status": "error",
            "latency_ms": _elapsed_ms(start),
            "error": f"probe timed out after {deadline}s",
        }
    except Exception as exc:  # noqa: BLE001 — every failure must be reported
        message = str(exc) or exc.__class__.__name__
        return {
            "status": "error",
            "latency_ms": _elapsed_ms(start),
            "error": message,
        }
    return {"status": "ok", "latency_ms": _elapsed_ms(start)}


def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 3)


# -- default (production) checkers -------------------------------------------


def default_checkers(
    *, trace_backend_transport: httpx.AsyncBaseTransport | None = None
) -> dict[str, Checker]:
    """Build the production reachability checkers from environment config.

    Each checker performs one lightweight call. A missing configuration
    value surfaces as a probe failure (the service is not ready), never as
    an endpoint crash. ``trace_backend_transport`` is a test-only injection
    seam (mirrors ``AuditBridgeClient``/``FhirClient``'s own ``transport=``
    seams) so tests can assert the "configured and reachable" path at the
    transport boundary instead of a real network call; omitted, a real
    ``httpx`` connection is used, exactly like the other two dependencies.
    """
    return {
        "openemr": make_openemr_checker(),
        "llm_provider": make_http_reachability_checker(
            LLM_PROVIDER_URL_ENV, dependency="llm_provider"
        ),
        "trace_backend": make_http_reachability_checker(
            OTEL_EXPORTER_OTLP_ENDPOINT_ENV,
            dependency="trace_backend",
            transport=trace_backend_transport,
        ),
    }


def default_deadline() -> float:
    """Overall probe deadline in seconds (env-configurable, default 5s)."""
    raw = os.environ.get(READINESS_DEADLINE_ENV)
    if raw is None:
        return DEFAULT_DEADLINE_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_DEADLINE_SECONDS
    return value if value > 0 else DEFAULT_DEADLINE_SECONDS


def make_openemr_checker(
    *, transport: httpx.AsyncBaseTransport | None = None
) -> Checker:
    """Checker probing OpenEMR's site root via a plain, unauthenticated GET.

    Deliberately *not* the FHIR API: ``<base>/metadata`` sits behind the
    same auth gate as the rest of the FHIR surface, so it 503s whenever the
    caller has no token — regardless of whether OpenEMR itself is actually
    up (T045). This checker instead derives OpenEMR's site root from
    ``OPENEMR_FHIR_BASE_URL`` (``scheme://netloc`` only, via
    :func:`urllib.parse.urlsplit`) and issues a bare GET with no
    ``Authorization`` header — mirroring
    :func:`make_http_reachability_checker`'s ``>=500 = failure`` semantics,
    but against a derived URL rather than an env var read verbatim.

    ``transport`` is the same test-only injection seam the other
    reachability checkers use (default ``None`` uses a real network
    connection).
    """

    async def check() -> None:
        base_url = os.environ.get(OPENEMR_FHIR_BASE_URL_ENV)
        if not base_url:
            raise RuntimeError(
                f"openemr base URL is not configured "
                f"(set {OPENEMR_FHIR_BASE_URL_ENV})"
            )
        root_url = _derive_openemr_root(base_url)
        if root_url is None:
            raise RuntimeError(
                f"openemr base URL is not a parseable URL "
                f"(missing scheme or host in {OPENEMR_FHIR_BASE_URL_ENV})"
            )
        async with httpx.AsyncClient(
            timeout=PROBE_HTTP_TIMEOUT_SECONDS, transport=transport
        ) as client:
            response = await client.get(root_url)
            if response.status_code >= 500:
                raise RuntimeError(
                    f"openemr responded with HTTP {response.status_code}"
                )

    return check


def _derive_openemr_root(base_url: str) -> str | None:
    """Derive OpenEMR's site root (``scheme://netloc``) from a FHIR base URL.

    Returns ``None`` when ``base_url`` is unparseable (empty ``scheme`` or
    ``netloc``), so the caller can fail closed via the same ``RuntimeError``
    path used for missing configuration, rather than crash.
    """
    parts = urlsplit(base_url)
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}"


def make_http_reachability_checker(
    env_var: str,
    *,
    dependency: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Checker:
    """Checker performing a GET against the URL configured in ``env_var``.

    ``transport`` is an injection seam (default ``None`` uses a real network
    connection) — tests substitute an ``httpx.MockTransport`` to assert the
    "configured and reachable" path at the transport seam, the same pattern
    ``AuditBridgeClient``/``FhirClient`` already use.
    """

    async def check() -> None:
        url = os.environ.get(env_var)
        if not url:
            raise RuntimeError(
                f"{dependency} URL is not configured (set {env_var})"
            )
        async with httpx.AsyncClient(
            timeout=PROBE_HTTP_TIMEOUT_SECONDS, transport=transport
        ) as client:
            response = await client.get(url)
            if response.status_code >= 500:
                raise RuntimeError(
                    f"{dependency} responded with HTTP {response.status_code}"
                )

    return check
