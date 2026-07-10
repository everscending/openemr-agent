"""Tests for observability — OTel spans, token/cost tracking, PHI-free logs
(T014).

Criteria map (see .tdd-swarm/tickets/T014-observability.md):
  1.  Each ``/chat`` request produces a trace: a root ``chat.request`` span
      with child spans for every tool call and every LLM call, asserted with
      OTel's ``InMemorySpanExporter`` — span names, parentage, and per-span
      duration fields present.
  2.  LLM-call spans carry model identifier, input/output tokens, and
      computed cost (from a configurable price table). Tool spans carry tool
      name and outcome (ok / typed-error class). Verification spans carry
      verdict counts.
  3.  Every span and every log record carries the request's correlation ID.
  4.  Portability guard: no module under ``agent/src/`` imports ``langsmith``
      or any vendor tracing SDK, enforced by a test that walks the import
      graph and fails on violation.
  5.  PHI-free logging guard: sentinel strings never appear in captured
      service log records or exported span attributes/names/events.
  6.  Tool-failure and audit-delivery counters are exposed on a ``/metrics``
      JSON endpoint; asserted to increment and to carry no request-scoped
      data (no patient id, conversation id, correlation id, or token).

Design decisions pinned here (orchestrator-authored, not relitigated):
  * OTel API (``opentelemetry.trace``) in instrumented code; the SDK and any
    exporter are imported only by ``copilot.telemetry.bootstrap``.
  * ``span.record_exception`` is never called; ``error.type`` is the
    exception class name only, span status carries no description.
  * Span names are static, low-cardinality constants
    (``chat.request``/``llm.call``/``tool.call``/``verify.response``).
  * Cost is ``Decimal``, computed from a configurable price table; absent
    (not zero) when the LLM reports no token usage or the model is unpriced.
  * ``/metrics`` exposes counters only — never patient/conversation/
    correlation identifiers or tokens.

Production code (the new ``copilot.telemetry`` package, and the ``tracer``/
``metrics`` injection seams on ``create_app``) is referenced lazily inside
test bodies where genuinely new, so collection succeeds before the
implementation exists (RED = the missing feature per test).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
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
from opentelemetry.trace import StatusCode

from copilot import contracts
from copilot.agent import ports
from copilot.agent.tools import Tool, ToolRegistry
from copilot.app import DEFAULT_CHAT_MODEL, create_app
from copilot.audit import AuditBridgeClient
from copilot.correlation import CORRELATION_ID_HEADER
from copilot.fhir import FhirNotFound

AWARE = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Lazy module accessors for the genuinely-new telemetry package
# ---------------------------------------------------------------------------


def import_guard_mod() -> Any:
    from copilot.telemetry import import_guard

    return import_guard


def pricing_mod() -> Any:
    from copilot.telemetry import pricing

    return pricing


def metrics_mod() -> Any:
    from copilot.telemetry import metrics

    return metrics


def bootstrap_mod() -> Any:
    from copilot.telemetry import bootstrap

    return bootstrap


def copilot_src_root() -> Path:
    import copilot

    return Path(copilot.__file__).resolve().parent


# ---------------------------------------------------------------------------
# Scripted fake LLM (mirrors test_agent_loop.py / test_chat.py's shape)
# ---------------------------------------------------------------------------


class ScriptedLLM:
    """Returns canned responses in order; records every call's arguments."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[SimpleNamespace] = []

    async def complete(self, *, system: str, messages: Any, tools: Any) -> Any:
        self.calls.append(SimpleNamespace(messages=list(messages)))
        if not self._responses:
            raise AssertionError("LLM called more times than scripted")
        return self._responses.pop(0)

    @property
    def call_count(self) -> int:
        return len(self.calls)


def final(
    text: str | None,
    *,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
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


# ---------------------------------------------------------------------------
# Fake tools
# ---------------------------------------------------------------------------


def observations_output(
    *,
    display: str = "Hemoglobin",
    value: str = "13.2 g/dL",
    resource_id: str = "obs-1",
) -> Any:
    return contracts.SearchObservationsOutput(
        records=(
            contracts.ObservationRecord(
                ref=contracts.ResourceRef(
                    resource_type="Observation", resource_id=resource_id
                ),
                code="718-7",
                display=display,
                value=value,
                effective=AWARE,
            ),
        )
    )


def make_tool(
    name: str, *, output: Any | None = None, raises: Exception | None = None
) -> Any:
    async def executor(validated: Any) -> Any:
        if raises is not None:
            raise raises
        return output if output is not None else observations_output()

    return Tool(
        name=name,
        description="A fake tool for T014 observability tests.",
        input_model=contracts.SearchObservationsInput,
        executor=executor,
    )


# ---------------------------------------------------------------------------
# App / tracer builders
# ---------------------------------------------------------------------------


def build_tracer(exporter: InMemorySpanExporter) -> Any:
    """A private ``TracerProvider`` per test — never the global provider, so
    exported spans from one test can never leak into another."""
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("copilot-tests")


def make_client(
    llm: Any,
    *,
    tools: tuple[Any, ...] = (),
    tracer: Any | None = None,
    metrics: Any | None = None,
    audit_bridge: Any | None = None,
    chat_model: str = DEFAULT_CHAT_MODEL,
) -> TestClient:
    kwargs: dict[str, Any] = {
        "chat_llm": llm,
        "chat_registry_factory": lambda token: ToolRegistry(tools),
        "chat_model": chat_model,
    }
    if tracer is not None:
        kwargs["tracer"] = tracer
    if metrics is not None:
        kwargs["metrics"] = metrics
    if audit_bridge is not None:
        kwargs["audit_bridge"] = audit_bridge
    return TestClient(create_app(**kwargs))


def chat_body(
    message: str = "Catch me up.",
    *,
    patient_id: str = "pat-1",
    token: str = "user-token-abc",
) -> dict[str, Any]:
    return {"message": message, "patient_id": patient_id, "token": token}


def span_by_name(exporter: InMemorySpanExporter, name: str) -> Any:
    return next(s for s in exporter.get_finished_spans() if s.name == name)


def spans_by_name(exporter: InMemorySpanExporter, name: str) -> list[Any]:
    return [s for s in exporter.get_finished_spans() if s.name == name]


# ==========================================================================
# Criterion 1 — root span + child spans for every tool/LLM call; parentage
# and duration fields (mandatory adversarial: parentage by walking ids)
# ==========================================================================


def test_chat_request_produces_root_span_with_llm_tool_and_verify_children() -> None:
    exporter = InMemorySpanExporter()
    tracer = build_tracer(exporter)
    tool = make_tool("search_observations")
    llm = ScriptedLLM(
        [
            tool_use("search_observations", {}),
            final(
                "Findings reviewed [Observation/obs-1].",
                input_tokens=1000,
                output_tokens=500,
            ),
        ]
    )
    client = make_client(llm, tools=(tool,), tracer=tracer)

    resp = client.post("/chat", json=chat_body())
    assert resp.status_code == 200

    root_spans = spans_by_name(exporter, "chat.request")
    assert len(root_spans) == 1
    root = root_spans[0]
    assert root.parent is None

    llm_spans = spans_by_name(exporter, "llm.call")
    tool_spans = spans_by_name(exporter, "tool.call")
    verify_spans = spans_by_name(exporter, "verify.response")
    assert len(llm_spans) == 2, exporter.get_finished_spans()
    assert len(tool_spans) == 1
    assert len(verify_spans) == 1

    # Parentage asserted by walking parent span ids, not by counting spans.
    root_id = root.context.span_id
    for s in (*llm_spans, *tool_spans, *verify_spans):
        assert s.parent is not None
        assert s.parent.span_id == root_id

    # Duration fields present on every span.
    for s in (root, *llm_spans, *tool_spans, *verify_spans):
        assert s.start_time is not None
        assert s.end_time is not None
        assert s.end_time >= s.start_time


def test_two_tool_calls_in_one_turn_are_siblings_not_nested_under_each_other() -> None:
    """My own adversarial probe: proves parentage is checked structurally per
    span (walking ids), not merely by counting root's direct children — two
    sibling tool calls must each point straight at root, never at one
    another."""
    exporter = InMemorySpanExporter()
    tracer = build_tracer(exporter)
    tool_a = make_tool("tool_a")
    tool_b = make_tool("tool_b")

    two_calls = ports.LLMResponse(
        stop_reason=ports.StopReason.TOOL_USE,
        tool_calls=(
            ports.ToolCallRequest(id="tc-1", name="tool_a", arguments={}),
            ports.ToolCallRequest(id="tc-2", name="tool_b", arguments={}),
        ),
    )
    llm = ScriptedLLM([two_calls, final("Reviewed [Observation/obs-1].")])
    client = make_client(llm, tools=(tool_a, tool_b), tracer=tracer)

    resp = client.post("/chat", json=chat_body())
    assert resp.status_code == 200

    root = span_by_name(exporter, "chat.request")
    tool_spans = spans_by_name(exporter, "tool.call")
    assert len(tool_spans) == 2

    root_id = root.context.span_id
    tool_span_ids = {s.context.span_id for s in tool_spans}
    for s in tool_spans:
        assert s.parent is not None
        assert s.parent.span_id == root_id
        assert s.parent.span_id not in tool_span_ids  # neither is the other's parent


# ==========================================================================
# Criterion 2 — LLM/tool/verify span attributes
# ==========================================================================


def test_llm_span_carries_model_tokens_and_computed_cost() -> None:
    exporter = InMemorySpanExporter()
    tracer = build_tracer(exporter)
    llm = ScriptedLLM([final("Reviewed the chart.", input_tokens=1000, output_tokens=500)])
    client = make_client(llm, tracer=tracer, chat_model="claude-opus-4-8")

    resp = client.post("/chat", json=chat_body())
    assert resp.status_code == 200

    llm_span = span_by_name(exporter, "llm.call")
    assert llm_span.attributes["model"] == "claude-opus-4-8"
    assert llm_span.attributes["llm.input_tokens"] == 1000
    assert llm_span.attributes["llm.output_tokens"] == 500
    # (1000 * 15 + 500 * 75) / 1_000_000 == 0.0525, exact Decimal arithmetic.
    assert Decimal(llm_span.attributes["llm.cost_usd"]) == Decimal("0.0525")


def test_tool_span_carries_tool_name_and_ok_outcome() -> None:
    exporter = InMemorySpanExporter()
    tracer = build_tracer(exporter)
    tool = make_tool("search_observations")
    llm = ScriptedLLM(
        [tool_use("search_observations", {}), final("Reviewed [Observation/obs-1].")]
    )
    client = make_client(llm, tools=(tool,), tracer=tracer)

    resp = client.post("/chat", json=chat_body())
    assert resp.status_code == 200

    tool_span = span_by_name(exporter, "tool.call")
    assert tool_span.attributes["tool.name"] == "search_observations"
    assert tool_span.attributes["outcome"] == "ok"


def test_verify_span_carries_verdict_counts_matching_the_response_body() -> None:
    exporter = InMemorySpanExporter()
    tracer = build_tracer(exporter)
    llm = ScriptedLLM([final("Uncited claim with a number 42 mg.")])
    client = make_client(llm, tracer=tracer)

    resp = client.post("/chat", json=chat_body())
    assert resp.status_code == 200
    counts = resp.json()["verification"]

    verify_span = span_by_name(exporter, "verify.response")
    assert verify_span.attributes["verify.claims_total"] == counts["claims_total"]
    assert verify_span.attributes["verify.claims_passed"] == counts["claims_passed"]
    assert verify_span.attributes["verify.claims_stripped"] == counts["claims_stripped"]


# ==========================================================================
# Mandatory adversarial — no token usage -> cost attribute absent (not 0)
# ==========================================================================


def test_llm_response_with_no_token_usage_has_no_cost_attribute() -> None:
    exporter = InMemorySpanExporter()
    tracer = build_tracer(exporter)
    llm = ScriptedLLM([final("Reviewed the chart.", input_tokens=None, output_tokens=None)])
    client = make_client(llm, tracer=tracer)

    resp = client.post("/chat", json=chat_body())
    assert resp.status_code == 200

    llm_span = span_by_name(exporter, "llm.call")
    assert "llm.cost_usd" not in llm_span.attributes
    assert "llm.input_tokens" not in llm_span.attributes
    assert "llm.output_tokens" not in llm_span.attributes


def test_cost_absent_for_a_model_with_no_price_table_entry_even_with_tokens() -> None:
    """My own adversarial probe: an unpriced model must never silently read
    as free or wrongly priced — cost stays absent even though real token
    counts are present."""
    exporter = InMemorySpanExporter()
    tracer = build_tracer(exporter)
    llm = ScriptedLLM(
        [final("Reviewed the chart.", input_tokens=1000, output_tokens=500)]
    )
    client = make_client(llm, tracer=tracer, chat_model="totally-unpriced-model-xyz")

    resp = client.post("/chat", json=chat_body())
    assert resp.status_code == 200

    llm_span = span_by_name(exporter, "llm.call")
    assert llm_span.attributes["llm.input_tokens"] == 1000
    assert llm_span.attributes["llm.output_tokens"] == 500
    assert "llm.cost_usd" not in llm_span.attributes


# ==========================================================================
# Criterion 3 — correlation ID on every span and every log record
# ==========================================================================


def test_correlation_id_present_on_every_span_and_every_log_record(
    caplog: pytest.LogCaptureFixture,
) -> None:
    exporter = InMemorySpanExporter()
    tracer = build_tracer(exporter)
    tool = make_tool("search_observations")
    llm = ScriptedLLM(
        [tool_use("search_observations", {}), final("Reviewed [Observation/obs-1].")]
    )
    client = make_client(llm, tools=(tool,), tracer=tracer)

    caplog.set_level(logging.INFO)
    resp = client.post(
        "/chat", json=chat_body(), headers={CORRELATION_ID_HEADER: "corr-fixed-777"}
    )
    assert resp.status_code == 200

    spans = exporter.get_finished_spans()
    assert len(spans) >= 4  # root + 1 llm + 1 tool + 1 verify
    for s in spans:
        assert s.attributes.get("correlation_id") == "corr-fixed-777", s.name

    assert len(caplog.records) >= 1
    for record in caplog.records:
        assert getattr(record, "correlation_id", None) == "corr-fixed-777"


# ==========================================================================
# Criterion 4 + mandatory adversarial — the import guard, proven to catch a
# planted violation (and a transitive one two hops away, T008's exact miss)
# ==========================================================================


def test_guard_rejects_a_planted_langsmith_import() -> None:
    ig = import_guard_mod()
    result = ig.scan_source("import langsmith\n")
    assert "langsmith" in result.banned


def test_guard_rejects_a_planted_otel_sdk_import() -> None:
    ig = import_guard_mod()
    result = ig.scan_source("from opentelemetry.sdk.trace import TracerProvider\n")
    assert "opentelemetry.sdk" in result.banned


def test_guard_accepts_a_clean_source() -> None:
    ig = import_guard_mod()
    result = ig.scan_source(
        "from __future__ import annotations\n"
        "import json\n"
        "from copilot.agent.ports import LLMClient\n"
    )
    assert result.banned == frozenset()


def test_guard_catches_a_transitive_two_hop_violation(tmp_path: Path) -> None:
    """Recreates T008's exact miss: module ``a`` has no direct banned import
    but imports first-party module ``b``, which does. A guard that only
    scans each file's own AST in isolation (T008's bug) would pass ``a``; the
    guard must walk the import graph and flag it anyway."""
    ig = import_guard_mod()
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "a.py").write_text("from pkg import b\n\n\ndef use() -> int:\n    return b.value\n")
    (pkg / "b.py").write_text("import langsmith\n\nvalue = 1\n")

    violations = ig.scan_tree(pkg, exempt_modules=frozenset())

    assert "pkg.a" in violations, violations
    assert "langsmith" in violations["pkg.a"]
    assert "pkg.b" in violations
    assert "langsmith" in violations["pkg.b"]


def test_guard_exempts_only_the_named_bootstrap_module_not_a_copycat(
    tmp_path: Path,
) -> None:
    """My own adversarial probe: the one-file SDK exemption must not become a
    blanket loophole — a second module doing the exact same import is still
    flagged."""
    ig = import_guard_mod()
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "bootstrap.py").write_text("from opentelemetry.sdk.trace import TracerProvider\n")
    (pkg / "not_exempt.py").write_text("from opentelemetry.sdk.trace import TracerProvider\n")

    violations = ig.scan_tree(pkg, exempt_modules=frozenset({"pkg.bootstrap"}))

    assert "pkg.bootstrap" not in violations
    assert "pkg.not_exempt" in violations
    assert "opentelemetry.sdk" in violations["pkg.not_exempt"]


def test_real_codebase_has_zero_import_guard_violations() -> None:
    """The enforcement test: without this, criterion 4 is untested against
    the actual tree."""
    ig = import_guard_mod()
    violations = ig.scan_tree(copilot_src_root())
    assert violations == {}, violations


# ==========================================================================
# Criterion 5 + mandatory adversarial — typed-error tool spans; PHI sentinels
# ==========================================================================


def test_tool_typed_error_sets_error_type_with_no_message_event_or_url(
    caplog: pytest.LogCaptureFixture,
) -> None:
    exporter = InMemorySpanExporter()
    tracer = build_tracer(exporter)
    leaked_url = "https://fhir.example.test/apis/default/fhir/Observation/should-not-leak"
    err = FhirNotFound(
        f"Observation not found at {leaked_url}",
        resource_type="Observation",
        url=leaked_url,
    )
    tool = make_tool("search_observations", raises=err)
    llm = ScriptedLLM(
        [
            tool_use("search_observations", {}),
            final("The observation is unavailable [Observation/obs-1]."),
        ]
    )
    client = make_client(llm, tools=(tool,), tracer=tracer)

    caplog.set_level(logging.DEBUG)
    resp = client.post("/chat", json=chat_body())
    assert resp.status_code == 200

    tool_span = span_by_name(exporter, "tool.call")
    assert tool_span.attributes["outcome"] == "FhirNotFound"
    assert tool_span.attributes.get("error.type") == "FhirNotFound"
    assert tool_span.status.status_code == StatusCode.ERROR
    assert tool_span.status.description is None
    assert len(tool_span.events) == 0  # never span.record_exception()

    for s in exporter.get_finished_spans():
        for value in s.attributes.values():
            assert leaked_url not in str(value)
        for event in s.events:
            assert leaked_url not in event.name
            for value in event.attributes.values():
                assert leaked_url not in str(value)

    for record in caplog.records:
        assert leaked_url not in record.getMessage()
        assert leaked_url not in str(record.__dict__)


def test_phi_sentinels_never_leak_into_spans_or_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Mandatory adversarial test: a patient name, a medication name, a lab
    value, the question text, the reply text, and the raw bearer token must
    appear in none of: span names, span attributes (keys or values), span
    events, or captured log records."""
    exporter = InMemorySpanExporter()
    tracer = build_tracer(exporter)

    sentinel_patient_name = "Xyzzy Plugh Sentinelson"
    sentinel_med_name = "SentinelMedaZolamQRS"
    sentinel_lab_value = "999.9 SentinelUnits"
    sentinel_reply_marker = "SentinelReplyTextMarkerAbc"
    sentinel_question = f"Please summarize the chart for {sentinel_patient_name}."
    secret_token = "SUPER-SECRET-BEARER-TOKEN-xyz789"

    tool_output = observations_output(display=sentinel_med_name, value=sentinel_lab_value)
    tool = make_tool("search_observations", output=tool_output)
    llm = ScriptedLLM(
        [
            tool_use("search_observations", {}),
            final(
                f"{sentinel_reply_marker} noted [Observation/obs-1].",
                input_tokens=10,
                output_tokens=10,
            ),
        ]
    )
    client = make_client(llm, tools=(tool,), tracer=tracer)

    caplog.set_level(logging.DEBUG)
    resp = client.post(
        "/chat", json=chat_body(sentinel_question, token=secret_token)
    )
    assert resp.status_code == 200

    haystacks: list[str] = []
    for s in exporter.get_finished_spans():
        haystacks.append(s.name)
        for key, value in s.attributes.items():
            haystacks.append(str(key))
            haystacks.append(str(value))
        for event in s.events:
            haystacks.append(event.name)
            for key, value in event.attributes.items():
                haystacks.append(str(key))
                haystacks.append(str(value))
    for record in caplog.records:
        haystacks.append(record.getMessage())
        haystacks.append(str(record.__dict__))

    blob = "\n".join(haystacks)
    for sentinel in (
        sentinel_patient_name,
        sentinel_med_name,
        sentinel_lab_value,
        sentinel_reply_marker,
        sentinel_question,
        secret_token,
    ):
        assert sentinel not in blob, sentinel


# ==========================================================================
# Criterion 6 — /metrics: counters, incrementing, no request-scoped data
# (mandatory adversarial: no patient id, conversation id, correlation id,
# token)
# ==========================================================================


def test_metrics_endpoint_counts_tool_failures_and_carries_no_request_scoped_data() -> None:
    metrics = metrics_mod().TelemetryMetrics()
    tool = make_tool(
        "search_observations",
        raises=FhirNotFound("nf", resource_type="Observation"),
    )
    llm = ScriptedLLM(
        [
            tool_use("search_observations", {}),
            final("The observation is unavailable, please view source records."),
        ]
    )
    client = make_client(llm, tools=(tool,), metrics=metrics)

    before = client.get("/metrics").json()

    resp = client.post(
        "/chat",
        json=chat_body(),
        headers={CORRELATION_ID_HEADER: "corr-metrics-1"},
    )
    assert resp.status_code == 200
    conversation_id = resp.json()["conversation_id"]

    after_resp = client.get("/metrics")
    assert after_resp.status_code == 200
    after = after_resp.json()

    assert after["tool_failure_total"] == before["tool_failure_total"] + 1
    assert all(isinstance(v, int) for v in after.values())

    blob = str(after)
    assert "pat-1" not in blob
    assert "corr-metrics-1" not in blob
    assert conversation_id not in blob
    assert "user-token-abc" not in blob


def test_metrics_endpoint_counts_audit_delivery_failures() -> None:
    metrics = metrics_mod().TelemetryMetrics()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "internal"})

    bridge = AuditBridgeClient(
        base_url="https://openemr.example.test/apis/default/copilot/audit-bridge",
        transport=httpx.MockTransport(handler),
        metrics=metrics,
    )
    llm = ScriptedLLM([final("Reviewed the chart, nothing further.")])
    client = make_client(llm, audit_bridge=bridge, metrics=metrics)

    before = client.get("/metrics").json()
    resp = client.post("/chat", json=chat_body())
    assert resp.status_code == 200
    after = client.get("/metrics").json()

    assert after["audit_delivery_failure_total"] == before["audit_delivery_failure_total"] + 1


# ==========================================================================
# Pure-unit tests — pricing and metrics modules (fast, no HTTP layer)
# ==========================================================================


def test_compute_cost_is_exact_decimal_matching_the_documented_formula() -> None:
    p = pricing_mod()
    table = {
        "test-model": p.ModelPricing(
            input_rate_per_million=Decimal("15"),
            output_rate_per_million=Decimal("75"),
        )
    }
    cost = p.compute_cost("test-model", 1000, 500, table)
    assert cost == Decimal("0.0525")
    assert isinstance(cost, Decimal)


def test_compute_cost_is_none_when_no_tokens_reported_at_all() -> None:
    p = pricing_mod()
    table = {
        "m": p.ModelPricing(
            input_rate_per_million=Decimal("1"), output_rate_per_million=Decimal("1")
        )
    }
    assert p.compute_cost("m", None, None, table) is None


def test_compute_cost_is_none_for_an_unpriced_model_even_with_tokens() -> None:
    p = pricing_mod()
    assert p.compute_cost("unknown-model", 100, 100, {}) is None


def test_telemetry_metrics_counts_each_kind_independently() -> None:
    m = metrics_mod().TelemetryMetrics()
    m.record_success()
    m.record_failure()
    m.record_failure()
    m.record_tool_failure()
    snap = m.snapshot()
    assert snap["audit_delivery_success_total"] == 1
    assert snap["audit_delivery_failure_total"] == 2
    assert snap["tool_failure_total"] == 1


def test_bootstrap_configure_tracing_is_idempotent_and_returns_a_tracer() -> None:
    from opentelemetry.trace import Tracer

    bootstrap = bootstrap_mod()
    tracer1 = bootstrap.configure_tracing()
    tracer2 = bootstrap.configure_tracing()
    assert isinstance(tracer1, Tracer)
    assert isinstance(tracer2, Tracer)
