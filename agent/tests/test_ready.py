"""Tests for the /ready readiness endpoint (T004, renamed in T045).

Covers the acceptance criteria:

1. Three dependencies probed via injectable checker functions.
2. All reachable -> 200 with per-dependency ``ok`` status and latency_ms.
3. Any unreachable -> 503, all three still listed, failing one(s)
   identifiable by key with a non-empty error string.
4. Probes run concurrently under an overall configurable deadline; a hung
   probe is reported as a timeout failure, not a hung endpoint.
5. The trace backend key is the generic ``trace_backend`` (no vendor name).

T045 adds coverage for the ``openemr`` dependency (renamed from
``openemr_fhir``): it now probes OpenEMR's site root via a plain,
unauthenticated GET instead of the FHIR ``/metadata`` endpoint, so a
locked-down FHIR API no longer makes ``/ready`` falsely report OpenEMR
itself as down.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Awaitable, Callable

import httpx
import pytest
from fastapi.testclient import TestClient

from copilot.app import create_app
from copilot.readiness import (
    OPENEMR_FHIR_BASE_URL_ENV,
    make_openemr_checker,
)

DEPENDENCY_KEYS = {"openemr", "llm_provider", "trace_backend"}

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
            "openemr": ok_checker(),
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
            "openemr": ok_checker(delay=0.03),
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
    slow = body["openemr"]["latency_ms"]
    fast = body["llm_provider"]["latency_ms"]
    assert slow >= 20  # ~30ms sleep must show up in the measured latency
    assert slow > fast


# -- Criterion 3: failures -> 503, identifiable by key ----------------------


def test_ready_single_failure_returns_503_listing_all_dependencies() -> None:
    client = make_client(
        {
            "openemr": ok_checker(),
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
    assert body["openemr"]["status"] == "ok"
    assert body["trace_backend"]["status"] == "ok"


def test_ready_two_failures_both_identifiable() -> None:
    client = make_client(
        {
            "openemr": failing_checker("openemr down"),
            "llm_provider": ok_checker(),
            "trace_backend": failing_checker("traces down"),
        }
    )
    response = client.get("/ready")
    assert response.status_code == 503
    body = response.json()
    assert set(body.keys()) == DEPENDENCY_KEYS
    for failed_key in ("openemr", "trace_backend"):
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
            "openemr": ok_checker(delay=0.2),
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
            "openemr": ok_checker(),
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
    assert body["openemr"]["status"] == "ok"
    assert body["trace_backend"]["status"] == "ok"


# -- Criterion 5: generic trace_backend key ----------------------------------


def test_ready_trace_backend_key_is_generic_no_vendor_names() -> None:
    client = make_client(
        {
            "openemr": ok_checker(),
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
        "OTEL_EXPORTER_OTLP_ENDPOINT",
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
            "openemr": ok_checker(),
            "llm_provider": ok_checker(),
            "trace_backend": ok_checker(),
        }
    )
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# ==========================================================================
# T045: `openemr` probes OpenEMR's site root, not FHIR /metadata
# ==========================================================================


def _requests_seen(transport_handler):
    """Wrap a handler, recording every request it sees (for assertions)."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return transport_handler(request)

    return seen, handler


# -- Criterion 1 + adversarial "auth-gate independence" ----------------------


def test_make_openemr_checker_sends_plain_unauthenticated_get_to_root(
    monkeypatch,
) -> None:
    monkeypatch.setenv(OPENEMR_FHIR_BASE_URL_ENV, "http://openemr/apis/default/fhir")

    def transport_handler(request: httpx.Request) -> httpx.Response:
        # Any request bearing an Authorization header (even an empty
        # bearer) must be rejected — proves the probe truly sends none.
        if "authorization" in request.headers:
            return httpx.Response(403)
        return httpx.Response(200)

    seen, handler = _requests_seen(transport_handler)
    checker = make_openemr_checker(transport=httpx.MockTransport(handler))

    asyncio.run(checker())  # must not raise

    assert len(seen) == 1
    request = seen[0]
    assert "authorization" not in request.headers
    assert request.url.host == "openemr"
    assert request.url.path in ("", "/")
    assert "/apis" not in str(request.url)
    assert "metadata" not in str(request.url)


# -- Adversarial "FHIR-down, root-up": the exact previously-false-failing scenario


def test_openemr_ok_when_fhir_path_5xx_but_root_reachable(monkeypatch) -> None:
    monkeypatch.setenv(OPENEMR_FHIR_BASE_URL_ENV, "http://openemr/apis/default/fhir")

    def transport_handler(request: httpx.Request) -> httpx.Response:
        # If the checker ever probed the FHIR path (a regression), it would
        # hit this 5xx/401 branch and the checker would (wrongly) raise.
        if request.url.path.startswith("/apis"):
            return httpx.Response(503)
        return httpx.Response(200)

    checker = make_openemr_checker(transport=httpx.MockTransport(transport_handler))

    asyncio.run(checker())  # must not raise: root, not /apis, is probed


def test_openemr_ok_when_fhir_path_401_but_root_reachable(monkeypatch) -> None:
    monkeypatch.setenv(OPENEMR_FHIR_BASE_URL_ENV, "http://openemr/apis/default/fhir")

    def transport_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/apis"):
            return httpx.Response(401)
        return httpx.Response(200)

    checker = make_openemr_checker(transport=httpx.MockTransport(transport_handler))

    asyncio.run(checker())  # must not raise


# -- Criterion 2 + adversarial "derivation table" ----------------------------


@pytest.mark.parametrize(
    "configured_base_url,expected_root",
    [
        ("http://openemr/apis/default/fhir", "http://openemr"),
        (
            "http://openemr.railway.internal/apis/default/fhir",
            "http://openemr.railway.internal",
        ),
        ("http://openemr:8080/apis/default/fhir", "http://openemr:8080"),
        ("http://openemr", "http://openemr"),  # already bare — idempotent
    ],
)
def test_make_openemr_checker_derives_root_from_configured_base_url(
    monkeypatch, configured_base_url: str, expected_root: str
) -> None:
    monkeypatch.setenv(OPENEMR_FHIR_BASE_URL_ENV, configured_base_url)

    def transport_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    seen, handler = _requests_seen(transport_handler)
    checker = make_openemr_checker(transport=httpx.MockTransport(handler))

    asyncio.run(checker())

    assert len(seen) == 1
    requested = str(seen[0].url).rstrip("/")
    assert requested == expected_root.rstrip("/")


# -- Criterion 3: missing config -> status: error, same shape as other deps -


def test_make_openemr_checker_missing_config_raises_naming_the_env_var(
    monkeypatch,
) -> None:
    monkeypatch.delenv(OPENEMR_FHIR_BASE_URL_ENV, raising=False)
    checker = make_openemr_checker()

    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(checker())

    message = str(exc_info.value)
    assert OPENEMR_FHIR_BASE_URL_ENV in message


def test_ready_missing_openemr_config_surfaces_as_error_status(monkeypatch) -> None:
    monkeypatch.delenv(OPENEMR_FHIR_BASE_URL_ENV, raising=False)

    client = TestClient(
        create_app(
            readiness_checkers={
                "openemr": make_openemr_checker(),
                "llm_provider": ok_checker(),
                "trace_backend": ok_checker(),
            }
        )
    )
    response = client.get("/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["openemr"]["status"] == "error"
    assert OPENEMR_FHIR_BASE_URL_ENV in body["openemr"]["error"]


# -- Criterion 4 + adversarial "malformed-URL fail-closed" -------------------


@pytest.mark.parametrize(
    "malformed_value",
    ["not-a-url", "", "   ", "://missing-scheme", "openemr-no-scheme-or-slashes"],
)
def test_make_openemr_checker_malformed_url_raises_not_crashes(
    monkeypatch, malformed_value: str
) -> None:
    if malformed_value == "":
        monkeypatch.delenv(OPENEMR_FHIR_BASE_URL_ENV, raising=False)
    else:
        monkeypatch.setenv(OPENEMR_FHIR_BASE_URL_ENV, malformed_value)
    checker = make_openemr_checker()

    with pytest.raises(RuntimeError):
        asyncio.run(checker())


def test_make_openemr_checker_malformed_url_message_distinguishes_from_unset(
    monkeypatch,
) -> None:
    monkeypatch.setenv(OPENEMR_FHIR_BASE_URL_ENV, "not-a-url")
    checker = make_openemr_checker()
    with pytest.raises(RuntimeError) as malformed_exc:
        asyncio.run(checker())

    monkeypatch.delenv(OPENEMR_FHIR_BASE_URL_ENV, raising=False)
    checker_unset = make_openemr_checker()
    with pytest.raises(RuntimeError) as unset_exc:
        asyncio.run(checker_unset())

    assert str(malformed_exc.value) != str(unset_exc.value)


def test_ready_malformed_openemr_url_is_well_formed_503_never_500(
    monkeypatch,
) -> None:
    monkeypatch.setenv(OPENEMR_FHIR_BASE_URL_ENV, "not-a-url")
    monkeypatch.setenv("LLM_PROVIDER_URL", "http://llm.example.test")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel.example.test")

    client = TestClient(
        create_app(
            readiness_checkers={
                "openemr": make_openemr_checker(),
                "llm_provider": ok_checker(),
                "trace_backend": ok_checker(),
            }
        )
    )
    response = client.get("/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["openemr"]["status"] == "error"
    assert isinstance(body["openemr"]["error"], str)
    assert body["openemr"]["error"] != ""


# -- Criterion 5: >=500 = failure, everything else = ok ----------------------


@pytest.mark.parametrize("status_code", [200, 204, 301, 302, 404, 403])
def test_make_openemr_checker_non_5xx_root_response_is_ok(
    monkeypatch, status_code: int
) -> None:
    monkeypatch.setenv(OPENEMR_FHIR_BASE_URL_ENV, "http://openemr/apis/default/fhir")

    def transport_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code)

    checker = make_openemr_checker(transport=httpx.MockTransport(transport_handler))
    asyncio.run(checker())  # must not raise


@pytest.mark.parametrize("status_code", [500, 502, 503])
def test_make_openemr_checker_5xx_root_response_raises(
    monkeypatch, status_code: int
) -> None:
    monkeypatch.setenv(OPENEMR_FHIR_BASE_URL_ENV, "http://openemr/apis/default/fhir")

    def transport_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code)

    checker = make_openemr_checker(transport=httpx.MockTransport(transport_handler))
    with pytest.raises(RuntimeError):
        asyncio.run(checker())


# -- Criterion 6: make_openemr_fhir_checker and FhirClient import removed ----


def test_make_openemr_fhir_checker_no_longer_exists() -> None:
    import copilot.readiness as readiness_module

    assert not hasattr(readiness_module, "make_openemr_fhir_checker")


def test_readiness_module_does_not_import_fhir_client() -> None:
    import copilot.readiness as readiness_module

    assert not hasattr(readiness_module, "FhirClient")


# -- Criterion 7 + adversarial "key-rename completeness" ---------------------


def test_no_openemr_fhir_references_remain_in_agent_or_docs() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    hits: list[str] = []
    for relative_base in ("agent", "docs"):
        base_path = repo_root / relative_base
        if not base_path.exists():
            continue
        for path in base_path.rglob("*"):
            if not path.is_file():
                continue
            if ".tdd-swarm" in path.parts:
                continue
            if "__pycache__" in path.parts or ".git" in path.parts:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, PermissionError):
                continue
            if "openemr_fhir" in text:
                hits.append(str(path.relative_to(repo_root)))
    assert hits == [], f"'openemr_fhir' still referenced in: {hits}"


def test_ready_response_body_never_contains_openemr_fhir_key(monkeypatch) -> None:
    monkeypatch.delenv(OPENEMR_FHIR_BASE_URL_ENV, raising=False)
    monkeypatch.delenv("LLM_PROVIDER_URL", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)

    client = TestClient(create_app())
    response = client.get("/ready")
    serialized = json.dumps(response.json())
    assert "openemr_fhir" not in serialized
    assert "openemr" in response.json()


# -- Criterion 9: existing /ready guarantees unchanged (regression via the
# renamed key — concurrency, deadline, timeout, 200-iff-all-ok already
# exercised above with the "openemr" key; this adds the DEPENDENCY_KEYS
# wiring check specifically for the rename).


def test_ready_default_checkers_use_openemr_key_not_openemr_fhir(monkeypatch) -> None:
    for var in (
        "OPENEMR_FHIR_BASE_URL",
        "LLM_PROVIDER_URL",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
    ):
        monkeypatch.delenv(var, raising=False)
    client = TestClient(create_app())
    response = client.get("/ready")
    body = response.json()
    assert "openemr" in body
    assert "openemr_fhir" not in body
