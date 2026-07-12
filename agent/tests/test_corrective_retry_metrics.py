"""Tests for T039 — ``llm_corrective_retry_total`` counter (a T014 amendment).

Criteria map (see .tdd-swarm/tickets/T039-*.md):
  1.  ``TelemetryMetrics`` gains a thread-safe ``record_corrective_retry()``
      method and a ``llm_corrective_retry_total`` key in ``snapshot()`` — a
      plain process counter, no request-scoped data.
  2.  The loop increments it at the corrective-retry site only: once per
      malformed-draft corrective branch (the first malformed draft, which is
      fed back for a retry) — never on the terminal ``MALFORMED_OUTPUT``
      fallback (the second consecutive malformed draft) and never on
      tool-argument failures. Asserted at the injection seam (a spy
      recorder), not merely via the loop's return value.
  3.  ``/metrics`` exposes it: a driven request with one corrective retry
      shows ``llm_corrective_retry_total == 1``; a clean request (no
      malformed draft) leaves it at 0; the snapshot stays counters-only.

Production code (``record_corrective_retry`` / ``llm_corrective_retry_total``,
and the loop's increment call) is referenced lazily / doesn't exist yet, so
collection succeeds before the implementation exists (RED = the missing
feature per test, not an import error).
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any

import pytest

from copilot import contracts
from copilot.agent import ports, tools
from copilot.agent.loop import AgentLoop, FallbackReason
from copilot.app import DEFAULT_CHAT_MODEL, create_app
from copilot.correlation import CORRELATION_ID_HEADER
from copilot.telemetry.metrics import TelemetryMetrics
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Scripted fake LLM + response builders (mirrors test_agent_loop.py's shape)
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


def final(text: str | None) -> Any:
    return ports.LLMResponse(stop_reason=ports.StopReason.END_TURN, text=text)


def tool_use(name: str, arguments: dict[str, Any], *, call_id: str = "tc-1") -> Any:
    return ports.LLMResponse(
        stop_reason=ports.StopReason.TOOL_USE,
        tool_calls=(ports.ToolCallRequest(id=call_id, name=name, arguments=arguments),),
    )


def make_registry(*tool_list: Any) -> tools.ToolRegistry:
    return tools.ToolRegistry(tool_list)


def build_loop(
    llm: Any,
    registry: tools.ToolRegistry,
    *,
    tool_metrics: Any | None = None,
    patient_id: str = "pat-1",
    correlation_id: str = "corr-1",
    max_steps: int = 6,
    model: str = "claude-opus-4-8",
) -> AgentLoop:
    return AgentLoop(
        llm=llm,
        registry=registry,
        patient_id=patient_id,
        model=model,
        correlation_id=correlation_id,
        max_steps=max_steps,
        tool_metrics=tool_metrics,
    )


# Bad arguments for search_observations: start > end -> the contract rejects.
BAD_RANGE = {
    "patient_id": "pat-1",
    "start": "2026-07-02T00:00:00+00:00",
    "end": "2026-07-01T00:00:00+00:00",
}


class _SpyRecorder:
    """Fake recorder wired at the exact seam the loop calls through — proves
    *what* the loop calls, not merely what its return value implies."""

    def __init__(self) -> None:
        self.tool_failures = 0
        self.corrective_retries = 0

    def record_tool_failure(self) -> None:
        self.tool_failures += 1

    def record_corrective_retry(self) -> None:
        self.corrective_retries += 1


# ==========================================================================
# Criterion 1 — TelemetryMetrics: record_corrective_retry() + snapshot key
# ==========================================================================


def test_telemetry_metrics_records_corrective_retry_in_snapshot() -> None:
    m = TelemetryMetrics()
    m.record_corrective_retry()
    m.record_corrective_retry()
    snap = m.snapshot()
    assert snap["llm_corrective_retry_total"] == 2


def test_telemetry_metrics_corrective_retry_starts_at_zero_and_is_independent() -> None:
    """My own adversarial probe: the new counter must not share state with the
    existing trio, in either direction."""
    m = TelemetryMetrics()
    assert m.snapshot()["llm_corrective_retry_total"] == 0

    m.record_tool_failure()
    m.record_success()
    m.record_failure()
    assert m.snapshot()["llm_corrective_retry_total"] == 0

    m.record_corrective_retry()
    snap = m.snapshot()
    assert snap["llm_corrective_retry_total"] == 1
    # Recording the new counter must not have bumped any existing counter.
    assert snap["tool_failure_total"] == 1
    assert snap["audit_delivery_success_total"] == 1
    assert snap["audit_delivery_failure_total"] == 1


def test_telemetry_metrics_record_corrective_retry_is_thread_safe() -> None:
    """Concurrent increments must all land — proves the lock is actually
    held, not merely present in the source."""
    m = TelemetryMetrics()
    n_threads = 20
    per_thread = 50

    def hammer() -> None:
        for _ in range(per_thread):
            m.record_corrective_retry()

    threads = [threading.Thread(target=hammer) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert m.snapshot()["llm_corrective_retry_total"] == n_threads * per_thread


# ==========================================================================
# Criterion 2 — the loop increments at the corrective-retry site only,
# asserted at the injection seam (a spy), not via the return value
# ==========================================================================


async def test_loop_records_one_corrective_retry_on_malformed_then_recovered_draft() -> None:
    spy = _SpyRecorder()
    llm = ScriptedLLM([final(None), final("I reviewed the labs.")])
    result = await build_loop(llm, make_registry(), tool_metrics=spy).run(
        "Catch me up."
    )

    assert not result.is_fallback
    assert spy.corrective_retries == 1
    assert spy.tool_failures == 0


async def test_loop_does_not_record_a_retry_for_the_terminal_malformed_fallback() -> None:
    """Two consecutive malformed drafts: the first is a corrective retry
    (counted), the second is the terminal MALFORMED_OUTPUT fallback (must
    NOT be counted again) — total stays at 1, not 2."""
    spy = _SpyRecorder()
    llm = ScriptedLLM([final(None), final("   ")])
    result = await build_loop(llm, make_registry(), tool_metrics=spy).run(
        "Catch me up."
    )

    assert result.is_fallback
    assert result.fallback.reason == FallbackReason.MALFORMED_OUTPUT
    assert spy.corrective_retries == 1


async def test_loop_never_records_a_corrective_retry_for_a_clean_run() -> None:
    spy = _SpyRecorder()
    llm = ScriptedLLM([final("Reviewed the chart, nothing further.")])
    result = await build_loop(llm, make_registry(), tool_metrics=spy).run(
        "Catch me up."
    )

    assert not result.is_fallback
    assert spy.corrective_retries == 0


async def test_loop_does_not_record_a_corrective_retry_for_tool_arg_failures() -> None:
    """Design decision pinned by the ticket: tool-argument failures are a
    distinct axis (``tool_failure_total`` / TOOL_ARGS_INVALID) and must never
    bump the corrective-retry counter."""
    tmod = tools

    async def executor(validated: Any) -> Any:
        return contracts.SearchObservationsOutput(records=())

    tool = tmod.Tool(
        name="search_observations",
        description="fake",
        input_model=contracts.SearchObservationsInput,
        executor=executor,
    )
    spy = _SpyRecorder()
    llm = ScriptedLLM([tool_use("search_observations", BAD_RANGE), tool_use("search_observations", BAD_RANGE)])
    result = await build_loop(llm, make_registry(tool), tool_metrics=spy).run(
        "Recent labs?"
    )

    assert result.is_fallback
    assert result.fallback.reason == FallbackReason.TOOL_ARGS_INVALID
    assert spy.corrective_retries == 0


async def test_loop_works_with_no_recorder_injected() -> None:
    """No tool_metrics supplied (production default None) must not raise —
    mirrors the existing NullToolFailureRecorder default behavior."""
    llm = ScriptedLLM([final(None), final("Reviewed the chart.")])
    result = await build_loop(llm, make_registry(), tool_metrics=None).run(
        "Catch me up."
    )
    assert not result.is_fallback


# ==========================================================================
# Criterion 3 — /metrics exposes the counter; counters-only; incrementing
# ==========================================================================


def make_client(llm: Any, *, tools_: tuple[Any, ...] = (), metrics: Any) -> TestClient:
    return TestClient(
        create_app(
            chat_llm=llm,
            chat_registry_factory=lambda token: tools.ToolRegistry(tools_),
            chat_model=DEFAULT_CHAT_MODEL,
            metrics=metrics,
        )
    )


def chat_body(
    message: str = "Catch me up.",
    *,
    patient_id: str = "pat-1",
    token: str = "user-token-abc",
) -> dict[str, Any]:
    return {"message": message, "patient_id": patient_id, "token": token}


def test_metrics_endpoint_counts_one_corrective_retry_from_a_driven_request() -> None:
    metrics = TelemetryMetrics()
    llm = ScriptedLLM([final(None), final("Reviewed the chart, nothing further.")])
    client = make_client(llm, metrics=metrics)

    before = client.get("/metrics").json()
    assert before["llm_corrective_retry_total"] == 0

    resp = client.post(
        "/chat", json=chat_body(), headers={CORRELATION_ID_HEADER: "corr-t039-1"}
    )
    assert resp.status_code == 200

    after = client.get("/metrics").json()
    assert after["llm_corrective_retry_total"] == 1


def test_metrics_endpoint_leaves_corrective_retry_at_zero_for_a_clean_request() -> None:
    metrics = TelemetryMetrics()
    llm = ScriptedLLM([final("Reviewed the chart, nothing further.")])
    client = make_client(llm, metrics=metrics)

    resp = client.post("/chat", json=chat_body())
    assert resp.status_code == 200

    after = client.get("/metrics").json()
    assert after["llm_corrective_retry_total"] == 0


def test_metrics_snapshot_stays_counters_only_with_the_new_key_present() -> None:
    """Mandatory adversarial: the new key must not smuggle in request-scoped
    data, and the whole snapshot (every key, not just the new one) stays
    plain-int counters."""
    metrics = TelemetryMetrics()
    llm = ScriptedLLM([final(None), final("Reviewed the chart, nothing further.")])
    client = make_client(llm, metrics=metrics)

    resp = client.post(
        "/chat",
        json=chat_body(patient_id="pat-secret-77", token="tok-should-not-leak"),
        headers={CORRELATION_ID_HEADER: "corr-should-not-leak"},
    )
    assert resp.status_code == 200
    conversation_id = resp.json()["conversation_id"]

    after = client.get("/metrics").json()
    assert "llm_corrective_retry_total" in after
    assert all(isinstance(v, int) for v in after.values())

    blob = str(after)
    assert "pat-secret-77" not in blob
    assert "corr-should-not-leak" not in blob
    assert conversation_id not in blob
    assert "tok-should-not-leak" not in blob
