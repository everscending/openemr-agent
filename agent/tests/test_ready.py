"""Tests for the /ready readiness endpoint (T004).

Covers the acceptance criteria:

1. Three dependencies probed via injectable checker functions.
2. All reachable -> 200 with per-dependency ``ok`` status and latency_ms.
3. Any unreachable -> 503, all three still listed, failing one(s)
   identifiable by key with a non-empty error string.
4. Probes run concurrently under an overall configurable deadline; a hung
   probe is reported as a timeout failure, not a hung endpoint.
5. The trace backend key is the generic ``trace_backend`` (no vendor name).
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Awaitable, Callable

from fastapi.testclient import TestClient

from copilot.app import create_app

DEPENDENCY_KEYS = {"openemr_fhir", "llm_provider", "trace_backend"}

Checker = Callable[[], Awaitable[None]]


def ok_checker(delay: float = 0.0) -> Checker:
    """Fake checker that succeeds, optionally after sleeping ``delay`` seconds."""

    async def check() -> None:
        if delay:
            await asyncio.sleep(delay)

    return check


def failing_checker(message: str) -> Checker:
    """Fake checker that always raises with ``message``."""

    async def check() -> None:
        raise RuntimeError(message)

    return check


def make_client(
    checkers: dict[str, Checker], deadline: float | None = None
) -> TestClient:
    kwargs: dict[str, object] = {"readiness_checkers": checkers}
    if deadline is not None:
        kwargs["readiness_deadline"] = deadline
    return TestClient(create_app(**kwargs))


# -- Criteria 1 + 2: all ok -> 200, keys, statuses, latencies ---------------


def test_ready_all_ok_returns_200_with_exact_keys() -> None:
    client = make_client(
        {
            "openemr_fhir": ok_checker(),
            "llm_provider": ok_checker(),
            "trace_backend": ok_checker(),
        }
    )
    response = client.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == DEPENDENCY_KEYS
    for key in DEPENDENCY_KEYS:
        assert body[key]["status"] == "ok"


def test_ready_reports_numeric_latency_reflecting_probe_duration() -> None:
    client = make_client(
        {
            "openemr_fhir": ok_checker(delay=0.03),
            "llm_provider": ok_checker(),
            "trace_backend": ok_checker(),
        }
    )
    response = client.get("/ready")
    assert response.status_code == 200
    body = response.json()
    for key in DEPENDENCY_KEYS:
        latency = body[key]["latency_ms"]
        assert isinstance(latency, (int, float))
        assert latency >= 0
    slow = body["openemr_fhir"]["latency_ms"]
    fast = body["llm_provider"]["latency_ms"]
    assert slow >= 20  # ~30ms sleep must show up in the measured latency
    assert slow > fast


# -- Criterion 3: failures -> 503, identifiable by key ----------------------


def test_ready_single_failure_returns_503_listing_all_dependencies() -> None:
    client = make_client(
        {
            "openemr_fhir": ok_checker(),
            "llm_provider": failing_checker("llm exploded"),
            "trace_backend": ok_checker(),
        }
    )
    response = client.get("/ready")
    assert response.status_code == 503
    body = response.json()
    assert set(body.keys()) == DEPENDENCY_KEYS
    assert body["llm_provider"]["status"] != "ok"
    error = body["llm_provider"]["error"]
    assert isinstance(error, str)
    assert error != ""
    assert body["openemr_fhir"]["status"] == "ok"
    assert body["trace_backend"]["status"] == "ok"


def test_ready_two_failures_both_identifiable() -> None:
    client = make_client(
        {
            "openemr_fhir": failing_checker("fhir down"),
            "llm_provider": ok_checker(),
            "trace_backend": failing_checker("traces down"),
        }
    )
    response = client.get("/ready")
    assert response.status_code == 503
    body = response.json()
    assert set(body.keys()) == DEPENDENCY_KEYS
    for failed_key in ("openemr_fhir", "trace_backend"):
        assert body[failed_key]["status"] != "ok"
        assert isinstance(body[failed_key]["error"], str)
        assert body[failed_key]["error"] != ""
    assert body["llm_provider"]["status"] == "ok"


# -- Criterion 4: concurrency + deadline -------------------------------------


def test_ready_probes_run_concurrently() -> None:
    # Three probes sleeping 0.2s each: serial execution would take >= 0.6s,
    # concurrent execution ~0.2s. Bound well below the serial sum.
    client = make_client(
        {
            "openemr_fhir": ok_checker(delay=0.2),
            "llm_provider": ok_checker(delay=0.2),
            "trace_backend": ok_checker(delay=0.2),
        }
    )
    start = time.perf_counter()
    response = client.get("/ready")
    elapsed = time.perf_counter() - start
    assert response.status_code == 200
    assert elapsed < 0.5, f"probes appear serialized (took {elapsed:.3f}s)"


def test_ready_hung_probe_yields_timeout_not_hung_endpoint() -> None:
    client = make_client(
        {
            "openemr_fhir": ok_checker(),
            "llm_provider": ok_checker(delay=5.0),  # hangs past the deadline
            "trace_backend": ok_checker(),
        },
        deadline=0.1,
    )
    start = time.perf_counter()
    response = client.get("/ready")
    elapsed = time.perf_counter() - start
    assert elapsed < 1.5, f"endpoint hung on a slow probe (took {elapsed:.3f}s)"
    assert response.status_code == 503
    body = response.json()
    assert set(body.keys()) == DEPENDENCY_KEYS
    hung = body["llm_provider"]
    assert hung["status"] != "ok"
    assert isinstance(hung["error"], str)
    assert "timeout" in hung["error"].lower() or "timed out" in hung["error"].lower()
    assert body["openemr_fhir"]["status"] == "ok"
    assert body["trace_backend"]["status"] == "ok"


# -- Criterion 5: generic trace_backend key ----------------------------------


def test_ready_trace_backend_key_is_generic_no_vendor_names() -> None:
    client = make_client(
        {
            "openemr_fhir": ok_checker(),
            "llm_provider": ok_checker(),
            "trace_backend": ok_checker(),
        }
    )
    response = client.get("/ready")
    body = response.json()
    assert "trace_backend" in body
    serialized = json.dumps(body).lower()
    for vendor in ("langsmith", "langfuse", "jaeger", "datadog", "honeycomb"):
        assert vendor not in serialized


# -- Wiring: defaults exist, /health unaffected -------------------------------


def test_ready_exists_with_default_checkers(monkeypatch) -> None:
    """create_app() with no args wires default (production) checkers.

    With no dependency URLs configured, /ready must exist (not 404) and
    report unreadiness rather than crash.
    """
    for var in (
        "OPENEMR_FHIR_BASE_URL",
        "LLM_PROVIDER_URL",
        "TRACE_BACKEND_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    client = TestClient(create_app())
    response = client.get("/ready")
    assert response.status_code in (200, 503)
    assert response.status_code != 404
    body = response.json()
    assert set(body.keys()) == DEPENDENCY_KEYS


def test_health_still_works_alongside_ready() -> None:
    client = make_client(
        {
            "openemr_fhir": ok_checker(),
            "llm_provider": ok_checker(),
            "trace_backend": ok_checker(),
        }
    )
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
