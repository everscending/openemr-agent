"""Tests for T028 — observability dashboard + trace-backend exporter wiring.

Criteria map (see .tdd-swarm/tickets/T028-observability-dashboard.md):
  1.  Exporter is configuration, not code: with ``OTEL_EXPORTER_OTLP_ENDPOINT``
      unset the service still boots and ``/ready`` reports the trace
      backend's *configured* state (sourced from that same env var, not an
      unrelated one); with it set, ``copilot.telemetry.bootstrap`` (the one
      SDK-holding module, T014) actually constructs and attaches a real
      ``OTLPSpanExporter`` — endpoint, headers, and service name all env-only.
      No vendor SDK import enters ``agent/src/copilot/`` (the T014 import
      guard, exercised directly here too).
  3.  Signal existence: a single driven ``/chat`` request produces every
      signal the dashboard's 8 required panels read (pre-spec resolution
      table in the ticket), asserted together so they are proven to coexist
      for one real request, not merely in isolated fixtures scattered across
      other tickets' test files.

Design decisions pinned here (do not relitigate):
  * The OTel SDK/exporter is only ever touched inside
    ``copilot.telemetry.bootstrap``; because that module installs a
    **process-global, idempotent** ``TracerProvider`` (and because
    importing ``copilot.app`` at all already triggers one such install via
    its module-level ``app = create_app()``), the *only* reliable way to
    observe what a fresh call to ``configure_tracing()`` actually wires is a
    clean subprocess — mirrors this repo's own import-purity idiom
    (``test_agent_loop.py``'s ``test_loop_module_imports_no_anthropic_or_httpx``)
    and the "import in a clean subprocess" guidance for guards that must not
    be fooled by already-primed process state.
  * ``/ready``'s ``trace_backend`` key is reused, not reinvented (T004); it
    is repointed at the *real* exporter-config env var so it can never
    disagree with what ``bootstrap`` actually wires.
  * Cost/verify/tool signals are individually covered elsewhere (T014,
    T039); this file's criterion-3 test recombines them into one driven
    request specifically to prove co-existence, per the ticket's own text:
    "must exist in the exported spans/metrics for a driven request."

Production code (``copilot.telemetry.bootstrap`` env-driven service name,
``copilot.readiness``'s trace_backend rewiring, and the
``opentelemetry-exporter-otlp-proto-http`` dependency) is referenced lazily
so collection succeeds before the implementation exists (RED = the missing
feature per test, not an import error).
"""

from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from copilot import contracts
from copilot.agent import ports
from copilot.agent.tools import Tool, ToolRegistry
from copilot.app import DEFAULT_CHAT_MODEL, create_app
from copilot.telemetry.metrics import TelemetryMetrics

# ---------------------------------------------------------------------------
# Subprocess helper — a fresh interpreter per call, so bootstrap's
# module-global ``_configured`` flag is never already-primed by an earlier
# test or by ``copilot.app``'s own module-level ``create_app()`` call.
# ---------------------------------------------------------------------------


def run_in_subprocess(code: str, *, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, (
        f"subprocess failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    return result.stdout


def base_env() -> dict[str, str]:
    import os

    # Strip any exporter/service-name config the parent test session may have
    # picked up, so each subprocess starts from a clean, known slate.
    env = dict(os.environ)
    env.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
    env.pop("OTEL_EXPORTER_OTLP_HEADERS", None)
    env.pop("OTEL_SERVICE_NAME", None)
    return env


# ==========================================================================
# Criterion 1 — exporter env unset: boots fine, no exporter wired
# ==========================================================================


def test_configure_tracing_attaches_no_span_processor_when_endpoint_unset() -> None:
    code = (
        "from copilot.telemetry.bootstrap import configure_tracing\n"
        "from opentelemetry import trace\n"
        "configure_tracing()\n"
        "provider = trace.get_tracer_provider()\n"
        "processors = provider._active_span_processor._span_processors\n"
        "assert processors == (), f'expected no processors, got {processors!r}'\n"
        "print('OK')\n"
    )
    env = base_env()
    out = run_in_subprocess(code, env=env)
    assert "OK" in out


def test_app_module_imports_cleanly_with_exporter_env_unset() -> None:
    """The service still boots: importing ``copilot.app`` (which resolves the
    default tracer via ``configure_tracing()`` at module import time) must
    not raise, and the resulting app must actually serve traffic."""
    code = (
        "from fastapi.testclient import TestClient\n"
        "from copilot.app import create_app\n"
        "client = TestClient(create_app())\n"
        "resp = client.get('/health')\n"
        "assert resp.status_code == 200, resp.status_code\n"
        "print('OK')\n"
    )
    env = base_env()
    out = run_in_subprocess(code, env=env)
    assert "OK" in out


# ==========================================================================
# Criterion 1 — exporter env set: bootstrap wires a real OTLP exporter
# (endpoint, headers, service name all env-only)
# ==========================================================================


def test_configure_tracing_attaches_real_otlp_exporter_when_endpoint_set() -> None:
    code = (
        "from copilot.telemetry.bootstrap import configure_tracing\n"
        "from opentelemetry import trace\n"
        "from opentelemetry.sdk.trace.export import SimpleSpanProcessor\n"
        "from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter\n"
        "configure_tracing()\n"
        "provider = trace.get_tracer_provider()\n"
        "processors = provider._active_span_processor._span_processors\n"
        "assert len(processors) == 1, processors\n"
        "proc = processors[0]\n"
        "assert isinstance(proc, SimpleSpanProcessor), type(proc)\n"
        "exporter = proc.span_exporter\n"
        "assert isinstance(exporter, OTLPSpanExporter), type(exporter)\n"
        "assert exporter._endpoint == 'http://collector.test:4318/v1/traces', exporter._endpoint\n"
        "print('OK')\n"
    )
    env = base_env()
    env["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://collector.test:4318/v1/traces"
    out = run_in_subprocess(code, env=env)
    assert "OK" in out


def test_configure_tracing_otlp_exporter_reads_headers_from_env() -> None:
    """Headers are 'selected entirely from env' too (criterion 1) — the OTLP
    exporter reads ``OTEL_EXPORTER_OTLP_HEADERS`` itself when bootstrap does
    not override ``headers=``; assert the real header actually lands on the
    constructed exporter, not merely that the env var was read somewhere."""
    code = (
        "from copilot.telemetry.bootstrap import configure_tracing\n"
        "from opentelemetry import trace\n"
        "configure_tracing()\n"
        "provider = trace.get_tracer_provider()\n"
        "exporter = provider._active_span_processor._span_processors[0].span_exporter\n"
        "assert exporter._headers.get('x-api-key') == 'secret-123', exporter._headers\n"
        "print('OK')\n"
    )
    env = base_env()
    env["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://collector.test:4318/v1/traces"
    env["OTEL_EXPORTER_OTLP_HEADERS"] = "x-api-key=secret-123"
    out = run_in_subprocess(code, env=env)
    assert "OK" in out


def test_configure_tracing_service_name_reads_env_when_set() -> None:
    code = (
        "from copilot.telemetry.bootstrap import configure_tracing\n"
        "from opentelemetry import trace\n"
        "configure_tracing()\n"
        "provider = trace.get_tracer_provider()\n"
        "name = provider.resource.attributes.get('service.name')\n"
        "assert name == 'copilot-custom', name\n"
        "print('OK')\n"
    )
    env = base_env()
    env["OTEL_SERVICE_NAME"] = "copilot-custom"
    out = run_in_subprocess(code, env=env)
    assert "OK" in out


def test_configure_tracing_service_name_defaults_to_copilot_when_env_unset() -> None:
    """My own adversarial probe: the env-driven service name must not lose the
    sane default — an unset ``OTEL_SERVICE_NAME`` must still resolve to
    ``copilot``, not ``unknown_service`` or a crash."""
    code = (
        "from copilot.telemetry.bootstrap import configure_tracing\n"
        "from opentelemetry import trace\n"
        "configure_tracing()\n"
        "provider = trace.get_tracer_provider()\n"
        "name = provider.resource.attributes.get('service.name')\n"
        "assert name == 'copilot', name\n"
        "print('OK')\n"
    )
    env = base_env()
    out = run_in_subprocess(code, env=env)
    assert "OK" in out


def test_import_guard_stays_green_with_the_otlp_dependency_installed() -> None:
    """Adding ``opentelemetry-exporter-otlp-proto-http`` as a real dependency
    must not open a new import-guard hole — re-run the T014 whole-tree scan
    in a clean subprocess (never the already-imported parent process, whose
    module cache could mask a violation)."""
    code = (
        "from pathlib import Path\n"
        "import copilot\n"
        "from copilot.telemetry import import_guard\n"
        "root = Path(copilot.__file__).resolve().parent\n"
        "violations = import_guard.scan_tree(root)\n"
        "assert violations == {}, violations\n"
        "print('OK')\n"
    )
    env = base_env()
    out = run_in_subprocess(code, env=env)
    assert "OK" in out


# ==========================================================================
# Criterion 1 — /ready's trace_backend key reflects the *real* exporter
# config state (OTEL_EXPORTER_OTLP_ENDPOINT), not an unrelated env var
# ==========================================================================


def test_ready_trace_backend_reports_not_configured_when_exporter_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from copilot import readiness

    monkeypatch.delenv(readiness.OTEL_EXPORTER_OTLP_ENDPOINT_ENV, raising=False)
    checkers = readiness.default_checkers()

    with pytest.raises(Exception) as exc_info:
        import asyncio

        asyncio.run(checkers["trace_backend"]())
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" in str(exc_info.value)


def test_ready_trace_backend_reports_ok_when_exporter_configured_and_reachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 'reachable' half, asserted at the transport seam (a MockTransport)
    rather than a real network call — mirrors this codebase's own pattern
    for testing outbound HTTP (``AuditBridgeClient``, ``FhirClient``)."""
    from copilot import readiness

    monkeypatch.setenv(
        readiness.OTEL_EXPORTER_OTLP_ENDPOINT_ENV, "https://collector.example.test/v1/traces"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    checker = readiness.make_http_reachability_checker(
        readiness.OTEL_EXPORTER_OTLP_ENDPOINT_ENV,
        dependency="trace_backend",
        transport=httpx.MockTransport(handler),
    )

    import asyncio

    asyncio.run(checker())  # must not raise


def test_ready_endpoint_surfaces_trace_backend_unconfigured_via_default_checkers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end through the real ``/ready`` route (default checkers, no
    injected fakes) — proves the wiring reaches the HTTP surface, not just
    the checker function in isolation."""
    for var in ("OPENEMR_FHIR_BASE_URL", "LLM_PROVIDER_URL", "OTEL_EXPORTER_OTLP_ENDPOINT"):
        monkeypatch.delenv(var, raising=False)

    client = TestClient(create_app())
    response = client.get("/ready")
    assert response.status_code != 404
    body = response.json()
    assert "trace_backend" in body
    assert body["trace_backend"]["status"] != "ok"
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" in body["trace_backend"]["error"]


# ==========================================================================
# Criterion 3 — every required panel's signal exists for one driven request
# (pre-spec resolution table, reproduced in docs/observability/README.md)
# ==========================================================================


class ScriptedLLM:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)

    async def complete(self, *, system: str, messages: Any, tools: Any) -> Any:
        if not self._responses:
            raise AssertionError("LLM called more times than scripted")
        return self._responses.pop(0)


def final(
    text: str | None, *, input_tokens: int | None = None, output_tokens: int | None = None
) -> Any:
    return ports.LLMResponse(
        stop_reason=ports.StopReason.END_TURN,
        text=text,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def tool_use(name: str, arguments: dict[str, Any], *, call_id: str = "tc-1") -> Any:
    return ports.LLMResponse(
        stop_reason=ports.StopReason.TOOL_USE,
        tool_calls=(ports.ToolCallRequest(id=call_id, name=name, arguments=arguments),),
    )


def observations_output() -> Any:
    return contracts.SearchObservationsOutput(
        records=(
            contracts.ObservationRecord(
                ref=contracts.ResourceRef(resource_type="Observation", resource_id="obs-1"),
                code="718-7",
                display="Hemoglobin",
                value="13.2 g/dL",
                effective=None,
            ),
        )
    )


def make_tool(name: str) -> Any:
    async def executor(validated: Any) -> Any:
        return observations_output()

    return Tool(
        name=name,
        description="fake tool for T028 dashboard signal coverage",
        input_model=contracts.SearchObservationsInput,
        executor=executor,
    )


def build_tracer(exporter: InMemorySpanExporter) -> Any:
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("copilot-tests-t028")


def test_all_eight_dashboard_panel_signals_present_for_one_driven_request() -> None:
    """One request that exercises a tool call, a corrective retry, and a
    priced final answer — then asserts every pre-spec panel's backing
    signal is present *together*, at once, for that single request:

      1. total request count      -> chat.request root span exists
      2. error rate                -> chat.request span has a status field
      3. p50/p95 latency            -> chat.request span start/end times
      4. tool-call counts           -> tool.call span + tool_failure_total key
      5. token cost                 -> llm.call span's llm.cost_usd attribute
      6. verification pass/fail     -> verify.response span's claim counts
      7. in-flight request depth    -> derived from #3's start/end (documented
                                        as backend-derived; same signal)
      8. retry counts                -> /metrics llm_corrective_retry_total
    """
    exporter = InMemorySpanExporter()
    tracer = build_tracer(exporter)
    metrics = TelemetryMetrics()
    tool = make_tool("search_observations")

    llm = ScriptedLLM(
        [
            tool_use("search_observations", {}),
            final(None),  # malformed draft -> one corrective retry (#8)
            final(
                "Findings reviewed [Observation/obs-1].",
                input_tokens=1000,
                output_tokens=500,
            ),
        ]
    )
    client = TestClient(
        create_app(
            chat_llm=llm,
            chat_registry_factory=lambda token: ToolRegistry((tool,)),
            chat_model=DEFAULT_CHAT_MODEL,
            tracer=tracer,
            metrics=metrics,
        )
    )

    resp = client.post(
        "/chat",
        json={"message": "Catch me up.", "patient_id": "pat-1", "token": "user-token-abc"},
    )
    assert resp.status_code == 200
    body = resp.json()

    spans = exporter.get_finished_spans()
    root = next(s for s in spans if s.name == "chat.request")
    # #1 total request count
    assert root is not None
    # #2 error rate — span carries a status object at all (backend counts by it)
    assert root.status is not None
    # #3 / #7 latency + in-flight depth — real start/end timestamps
    assert root.start_time is not None
    assert root.end_time is not None
    assert root.end_time >= root.start_time

    # #4 tool-call counts
    tool_span = next(s for s in spans if s.name == "tool.call")
    assert tool_span.attributes["tool.name"] == "search_observations"
    assert tool_span.attributes["outcome"] == "ok"
    metrics_body = client.get("/metrics").json()
    assert "tool_failure_total" in metrics_body

    # #5 token cost
    llm_spans = [s for s in spans if s.name == "llm.call"]
    priced = [s for s in llm_spans if "llm.cost_usd" in s.attributes]
    assert len(priced) == 1, llm_spans

    # #6 verification pass/fail
    verify_span = next(s for s in spans if s.name == "verify.response")
    assert verify_span.attributes["verify.claims_total"] == body["verification"]["claims_total"]
    assert verify_span.attributes["verify.claims_passed"] == body["verification"]["claims_passed"]

    # #8 retry counts
    assert metrics_body["llm_corrective_retry_total"] == 1
