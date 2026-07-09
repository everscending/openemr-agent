"""Readiness probing for the Co-Pilot service (T004).

The ``/ready`` endpoint validates three meaningful dependencies —
``openemr_fhir``, ``llm_provider``, and ``trace_backend`` — via injectable
async checker functions. This module owns concurrency, the overall
deadline, per-probe latency measurement, and response shaping; checkers
only perform a lightweight reachability call (or raise on failure).

Per ARCHITECTURE.md section 7 portability rule (b), the trace backend is
reported under the generic ``trace_backend`` key — a configured
dependency, never a named vendor product.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Awaitable, Callable, Mapping

import httpx

from copilot.fhir.client import FhirClient

Checker = Callable[[], Awaitable[None]]
"""Async callable that returns on success and raises on failure."""

DEPENDENCY_KEYS: tuple[str, ...] = ("openemr_fhir", "llm_provider", "trace_backend")

DEFAULT_DEADLINE_SECONDS = 5.0
PROBE_HTTP_TIMEOUT_SECONDS = 4.0

OPENEMR_FHIR_BASE_URL_ENV = "OPENEMR_FHIR_BASE_URL"
LLM_PROVIDER_URL_ENV = "LLM_PROVIDER_URL"
TRACE_BACKEND_URL_ENV = "TRACE_BACKEND_URL"
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


def default_checkers() -> dict[str, Checker]:
    """Build the production reachability checkers from environment config.

    Each checker performs one lightweight call. A missing configuration
    value surfaces as a probe failure (the service is not ready), never as
    an endpoint crash.
    """
    return {
        "openemr_fhir": make_openemr_fhir_checker(),
        "llm_provider": make_http_reachability_checker(
            LLM_PROVIDER_URL_ENV, dependency="llm_provider"
        ),
        "trace_backend": make_http_reachability_checker(
            TRACE_BACKEND_URL_ENV, dependency="trace_backend"
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


def make_openemr_fhir_checker(fhir_client: FhirClient | None = None) -> Checker:
    """Checker probing the OpenEMR FHIR endpoint via ``GET <base>/metadata``.

    An explicit :class:`FhirClient` may be injected; otherwise one is built
    per probe from ``OPENEMR_FHIR_BASE_URL`` (the CapabilityStatement
    endpoint requires no token, so an empty bearer token is used).
    """

    async def check() -> None:
        if fhir_client is not None:
            await fhir_client.capability_statement()
            return
        base_url = os.environ.get(OPENEMR_FHIR_BASE_URL_ENV)
        if not base_url:
            raise RuntimeError(
                f"openemr_fhir base URL is not configured "
                f"(set {OPENEMR_FHIR_BASE_URL_ENV})"
            )
        async with FhirClient(
            base_url, token="", timeout=PROBE_HTTP_TIMEOUT_SECONDS
        ) as client:
            await client.capability_statement()

    return check


def make_http_reachability_checker(env_var: str, *, dependency: str) -> Checker:
    """Checker performing a GET against the URL configured in ``env_var``."""

    async def check() -> None:
        url = os.environ.get(env_var)
        if not url:
            raise RuntimeError(
                f"{dependency} URL is not configured (set {env_var})"
            )
        async with httpx.AsyncClient(timeout=PROBE_HTTP_TIMEOUT_SECONDS) as client:
            response = await client.get(url)
            if response.status_code >= 500:
                raise RuntimeError(
                    f"{dependency} responded with HTTP {response.status_code}"
                )

    return check
