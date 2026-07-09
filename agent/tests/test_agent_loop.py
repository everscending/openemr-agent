"""Tests for the agent loop — tool use, malformed output, refusal boundary (T010).

Criteria map (see .tdd-swarm/tickets/T010-agent-loop.md):
  1.  An ``LLMClient`` protocol exists; the loop depends only on it; tests drive
      a scripted fake; no test makes network calls.
  2.  Loop mechanics: conversation + tool schemas -> LLM; tool-call turn is
      validated, executed, appended, continued; final draft is verified via
      T008/T009 and the verified response is returned. End-to-end two-step.
  3.  Bad tool arguments are fed back once; a second consecutive failure ends
      the loop with the structured fallback (no exception escapes).
  4.  Malformed final output -> one corrective retry -> second failure yields a
      typed ``FallbackRequired`` result.
  5.  A hard step cap (default 6) terminates the loop with the fallback.
  6.  Patient binding: every executed tool call uses the bound patient_id, and
      NO request for another patient reaches the transport (spy at the seam).
  7.  Tool execution failures (T003 typed errors) become a typed "tool
      unavailable" turn and surface in the final response's coverage.
  8.  Every LLM and tool turn is in a structured transcript keyed by
      correlation ID (role/name/timings/token counts).
  9.  The injected system prompt states the §5 citation contract (cite
      ``[ResourceType/id]`` for every resource relied upon) plus refusal rules.
  10. Refusal is a distinct terminal branch: structured fallback, partial
      content discarded (never rendered).
  11. The system prompt is static: it contains no patient identifier; the bound
      patient_id is supplied in the first user message.
  +   Import purity: importing the loop module pulls no ``anthropic`` and no
      ``httpx`` into ``sys.modules`` (verified in a clean subprocess).
  +   The SDK adapter constructor-tests only and implements the port.

Production code is imported lazily inside test bodies so collection succeeds
before the implementation exists (RED = the missing feature per test).
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from copilot import contracts
from copilot.fhir import FhirClient, FhirTimeout

AWARE = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)
BASE_URL = "https://emr.example.test/apis/default/fhir"
TOKEN = "user-token"


# ---------------------------------------------------------------------------
# Lazy module accessors (called inside bodies, never at collection time)
# ---------------------------------------------------------------------------


def loop_mod() -> Any:
    from copilot.agent import loop

    return loop


def ports_mod() -> Any:
    from copilot.agent import ports

    return ports


def transcript_mod() -> Any:
    from copilot.agent import transcript

    return transcript


def tools_mod() -> Any:
    from copilot.agent import tools

    return tools


def registry_mod() -> Any:
    from copilot.agent import registry

    return registry


# ---------------------------------------------------------------------------
# Scripted fake LLM clients (implement the LLMClient port)
# ---------------------------------------------------------------------------


class ScriptedLLM:
    """Returns canned responses in order; records every call's arguments."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[SimpleNamespace] = []

    async def complete(
        self, *, system: str, messages: Any, tools: Any
    ) -> Any:
        self.calls.append(
            SimpleNamespace(
                system=system, messages=list(messages), tools=list(tools)
            )
        )
        if not self._responses:
            raise AssertionError("LLM called more times than scripted")
        return self._responses.pop(0)

    @property
    def call_count(self) -> int:
        return len(self.calls)


class LoopingLLM:
    """Always requests the same tool call — never terminates on its own."""

    def __init__(self, response: Any) -> None:
        self._response = response
        self.call_count = 0

    async def complete(self, *, system: str, messages: Any, tools: Any) -> Any:
        self.call_count += 1
        return self._response


# ---------------------------------------------------------------------------
# Response builders
# ---------------------------------------------------------------------------


def tool_use(
    name: str, arguments: dict[str, Any], *, call_id: str = "tc-1"
) -> Any:
    p = ports_mod()
    return p.LLMResponse(
        stop_reason=p.StopReason.TOOL_USE,
        tool_calls=(
            p.ToolCallRequest(id=call_id, name=name, arguments=arguments),
        ),
    )


def final(
    text: str | None,
    *,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> Any:
    p = ports_mod()
    return p.LLMResponse(
        stop_reason=p.StopReason.END_TURN,
        text=text,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def refusal(text: str | None = None) -> Any:
    p = ports_mod()
    return p.LLMResponse(stop_reason=p.StopReason.REFUSAL, text=text)


def max_tokens(text: str | None = None) -> Any:
    p = ports_mod()
    return p.LLMResponse(stop_reason=p.StopReason.MAX_TOKENS, text=text)


# ---------------------------------------------------------------------------
# Fake tools + registry
# ---------------------------------------------------------------------------


def observation_output(*, empty: bool = False) -> Any:
    if empty:
        return contracts.SearchObservationsOutput(
            records=(),
            receipt=contracts.QueryReceipt(
                query_description="Observation?patient=pat-1",
                scope="observations for patient pat-1",
                timestamp=AWARE,
            ),
        )
    return contracts.SearchObservationsOutput(
        records=(
            contracts.ObservationRecord(
                ref=contracts.ResourceRef(
                    resource_type="Observation", resource_id="obs-1"
                ),
                code="718-7",
                display="Hemoglobin",
                value="13.2 g/dL",
                effective=AWARE,
            ),
        )
    )


def obs_tool(
    *, output: Any | None = None, raises: Exception | None = None, seen: dict | None = None
) -> Any:
    tmod = tools_mod()

    async def executor(validated: Any) -> Any:
        if seen is not None:
            seen["patient_id"] = validated.patient_id
        if raises is not None:
            raise raises
        return output if output is not None else observation_output()

    return tmod.Tool(
        name="search_observations",
        description="Search this patient's observations by code/category/date.",
        input_model=contracts.SearchObservationsInput,
        executor=executor,
    )


def make_registry(*tools: Any) -> Any:
    return tools_mod().ToolRegistry(tools)


# ---------------------------------------------------------------------------
# Loop construction helper
# ---------------------------------------------------------------------------


def build_loop(
    llm: Any,
    registry: Any,
    *,
    patient_id: str = "pat-1",
    correlation_id: str = "corr-1",
    max_steps: int = 6,
    model: str = "claude-opus-4-8",
) -> Any:
    return loop_mod().AgentLoop(
        llm=llm,
        registry=registry,
        patient_id=patient_id,
        model=model,
        correlation_id=correlation_id,
        max_steps=max_steps,
    )


# ==========================================================================
# Criterion 1 — LLMClient protocol; scripted fake; no network
# ==========================================================================


def test_llm_client_protocol_is_satisfied_by_a_scripted_fake() -> None:
    p = ports_mod()
    assert isinstance(ScriptedLLM([]), p.LLMClient)
    assert isinstance(LoopingLLM(None), p.LLMClient)


async def test_loop_returns_a_result_driven_entirely_by_the_fake() -> None:
    llm = ScriptedLLM([final("I reviewed the labs.")])
    result = await build_loop(llm, make_registry()).run("Catch me up.")
    assert llm.call_count == 1
    assert not result.is_fallback


# ==========================================================================
# Criterion 2 — loop mechanics end-to-end (tool-call -> final answer)
# ==========================================================================


async def test_tool_call_then_final_answer_is_executed_and_verified() -> None:
    seen: dict = {}
    llm = ScriptedLLM(
        [
            tool_use("search_observations", {"patient_id": "pat-1", "code": "718-7"}),
            final("Her hemoglobin was 13.2 [Observation/obs-1]."),
        ]
    )
    registry = make_registry(obs_tool(seen=seen))
    result = await build_loop(llm, registry).run("What was her last hemoglobin?")

    # The tool actually ran (its patient was captured) ...
    assert seen["patient_id"] == "pat-1"
    # ... and the LLM was handed the tool registry schema + the conversation.
    assert any(t.name == "search_observations" for t in llm.calls[0].tools)
    assert any("hemoglobin" in m.content.lower() for m in llm.calls[0].messages)
    # ... and the final draft was verified against the ref the tool returned.
    assert isinstance(result.verdict, contracts.VerificationVerdict)
    assert not result.verdict.fallback_triggered
    assert "[Observation/obs-1]" in result.output_text
    assert result.verdict.counts.claims_passed == 1
    assert not result.is_fallback


async def test_uncited_claim_in_final_draft_is_stripped_by_verification() -> None:
    # The loop must actually run verification, not merely echo the draft.
    llm = ScriptedLLM(
        [final("She is allergic to penicillin.")]  # a claim, no citation
    )
    result = await build_loop(llm, make_registry()).run("Any allergies?")
    assert result.verdict is not None
    assert result.verdict.fallback_triggered  # nothing verifiable survived
    assert "allergic to penicillin" not in result.output_text


# ==========================================================================
# Criterion 3 — bad tool args: fed back once; second consecutive -> fallback
# ==========================================================================


BAD_RANGE = {
    "patient_id": "pat-1",
    "start": "2026-07-02T00:00:00+00:00",
    "end": "2026-07-01T00:00:00+00:00",  # start > end -> contract rejects
}


async def test_bad_tool_args_are_fed_back_once_then_the_loop_recovers() -> None:
    seen: dict = {}
    llm = ScriptedLLM(
        [
            tool_use("search_observations", BAD_RANGE),
            tool_use("search_observations", {"patient_id": "pat-1"}),
            final("I reviewed the labs."),
        ]
    )
    registry = make_registry(obs_tool(seen=seen))
    result = await build_loop(llm, registry).run("Recent labs?")

    assert not result.is_fallback
    assert seen.get("patient_id") == "pat-1"  # the good call executed
    # The validation error was fed back to the LLM before it retried.
    second_turn = llm.calls[1].messages
    assert any(getattr(m, "is_error", False) for m in second_turn)


async def test_two_consecutive_bad_args_end_the_loop_with_fallback() -> None:
    llm = ScriptedLLM(
        [
            tool_use("search_observations", BAD_RANGE),
            tool_use("search_observations", BAD_RANGE),
        ]
    )
    registry = make_registry(obs_tool())
    result = await build_loop(llm, registry).run("Recent labs?")

    assert result.is_fallback
    assert result.fallback.reason == loop_mod().FallbackReason.TOOL_ARGS_INVALID
    assert llm.call_count == 2  # ended on the second consecutive failure


# ==========================================================================
# Criterion 4 — malformed final output: retry once, then FallbackRequired
# ==========================================================================


async def test_malformed_output_retried_once_then_recovers() -> None:
    llm = ScriptedLLM(
        [final(None), final("I reviewed the labs.")]  # empty draft, then valid
    )
    result = await build_loop(llm, make_registry()).run("Catch me up.")

    assert not result.is_fallback
    assert llm.call_count == 2
    # A corrective instruction was injected before the retry.
    assert any(
        "final answer" in m.content.lower() or "citation" in m.content.lower()
        for m in llm.calls[1].messages
    )


async def test_malformed_output_twice_yields_fallback_required() -> None:
    llm = ScriptedLLM([final(None), final("   ")])  # empty, then blank
    result = await build_loop(llm, make_registry()).run("Catch me up.")

    assert result.is_fallback
    assert result.fallback.reason == loop_mod().FallbackReason.MALFORMED_OUTPUT
    assert llm.call_count == 2


# ==========================================================================
# Criterion 5 — hard step cap terminates with the fallback
# ==========================================================================


async def test_step_cap_terminates_a_tool_calling_loop() -> None:
    seen: dict = {}
    llm = LoopingLLM(
        tool_use("search_observations", {"patient_id": "pat-1"})
    )
    registry = make_registry(obs_tool(seen=seen))
    result = await build_loop(llm, registry, max_steps=6).run("Loop forever.")

    assert result.is_fallback
    assert result.fallback.reason == loop_mod().FallbackReason.STEP_CAP_EXCEEDED
    assert llm.call_count == 6


async def test_step_cap_is_configurable() -> None:
    llm = LoopingLLM(tool_use("search_observations", {"patient_id": "pat-1"}))
    registry = make_registry(obs_tool())
    result = await build_loop(llm, registry, max_steps=3).run("Loop forever.")

    assert result.is_fallback
    assert llm.call_count == 3


# ==========================================================================
# Criterion 6 — patient binding is the security boundary (spy the transport)
# ==========================================================================


def spy_transport(seen: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path.endswith(f"/Patient/{request.url.params.get('_id', '')}"):
            return httpx.Response(200, json={"resourceType": "Patient", "id": "x"})
        # Empty searchset for everything else.
        return httpx.Response(
            200, json={"resourceType": "Bundle", "type": "searchset"}
        )

    return httpx.MockTransport(handler)


async def test_tool_executes_with_bound_patient_not_the_requested_one() -> None:
    seen: list[httpx.Request] = []
    client = FhirClient(BASE_URL, TOKEN, transport=spy_transport(seen))
    registry = registry_mod().build_default_registry(client)

    # The fake LLM tries to pull a DIFFERENT patient's chart.
    llm = ScriptedLLM(
        [
            tool_use(
                "search_observations",
                {"patient_id": "victim-999", "code": "718-7"},
            ),
            final("I reviewed the labs."),
        ]
    )
    async with client:
        result = await build_loop(llm, registry, patient_id="pat-1").run(
            "Show me this patient's labs."
        )

    assert not result.is_fallback
    assert seen, "the tool never reached the transport at all"
    urls = [str(r.url) for r in seen]
    # No request for the other patient EVER reached the transport ...
    assert all("victim-999" not in u for u in urls), urls
    # ... and the request that did go out carried the bound patient.
    assert any("patient=pat-1" in u for u in urls), urls


async def test_snapshot_tool_is_also_bound_to_the_open_chart() -> None:
    seen: list[httpx.Request] = []
    client = FhirClient(BASE_URL, TOKEN, transport=spy_transport(seen))
    registry = registry_mod().build_default_registry(client)
    llm = ScriptedLLM(
        [
            tool_use("get_patient_snapshot", {"patient_id": "victim-42"}),
            final("I reviewed the chart."),
        ]
    )
    async with client:
        await build_loop(llm, registry, patient_id="pat-1").run("Catch me up.")

    urls = [str(r.url) for r in seen]
    assert seen
    assert all("victim-42" not in u for u in urls), urls


# ==========================================================================
# Criterion 7 — tool failure -> typed "unavailable" turn + coverage
# ==========================================================================


async def test_tool_execution_failure_becomes_unavailable_and_never_crashes() -> None:
    err = FhirTimeout(
        "FHIR request for Observation timed out after 5.0s",
        resource_type="Observation",
    )
    llm = ScriptedLLM(
        [
            tool_use("search_observations", {"patient_id": "pat-1"}),
            final("I reviewed the labs."),
        ]
    )
    registry = make_registry(obs_tool(raises=err))
    result = await build_loop(llm, registry).run("Recent labs?")

    # Never crashed; surfaced in coverage.
    assert not result.is_fallback
    by_category = {c.category: c for c in result.coverage}
    assert "search_observations" in by_category
    entry = by_category["search_observations"]
    assert isinstance(entry, contracts.CoverageUnavailable)
    assert entry.reason  # not silent
    # The failure was fed back to the LLM as a tool result (never silent).
    tool_msgs = [
        m for m in llm.calls[1].messages if getattr(m, "role", None) == "tool"
    ]
    assert any(getattr(m, "is_error", False) for m in tool_msgs)


# ==========================================================================
# Criterion 8 — structured transcript keyed by correlation ID
# ==========================================================================


async def test_transcript_records_llm_and_tool_turns_by_correlation_id() -> None:
    llm = ScriptedLLM(
        [
            tool_use("search_observations", {"patient_id": "pat-1"}),
            final(
                "Her hemoglobin was 13.2 [Observation/obs-1].",
                input_tokens=1200,
                output_tokens=40,
            ),
        ]
    )
    registry = make_registry(obs_tool())
    result = await build_loop(
        llm, registry, correlation_id="corr-abc"
    ).run("Last hemoglobin?")

    t = result.transcript
    tmod = transcript_mod()
    assert t.correlation_id == "corr-abc"

    llm_entries = [e for e in t.entries if e.kind == tmod.TranscriptEntryKind.LLM]
    tool_entries = [e for e in t.entries if e.kind == tmod.TranscriptEntryKind.TOOL]
    assert len(llm_entries) == 2
    assert len(tool_entries) == 1
    assert tool_entries[0].name == "search_observations"
    assert all(e.name == "claude-opus-4-8" for e in llm_entries)
    # Timings present on every entry.
    for e in t.entries:
        assert e.started_at.tzinfo is not None
        assert e.duration_seconds >= 0.0
    # Token counts recorded when the LLM provides them.
    final_entry = llm_entries[-1]
    assert final_entry.input_tokens == 1200
    assert final_entry.output_tokens == 40


# ==========================================================================
# Criterion 9 — system prompt states the citation contract + refusal rules
# ==========================================================================


def test_system_prompt_states_the_citation_contract() -> None:
    prompt = loop_mod().SYSTEM_PROMPT
    assert "[ResourceType/id]" in prompt
    lowered = prompt.lower()
    # Every resource the claim relies on — not merely the one it is about.
    assert "every resource" in lowered
    assert "claim" in lowered


def test_system_prompt_carries_the_refusal_boundary() -> None:
    lowered = loop_mod().SYSTEM_PROMPT.lower()
    # USER.md §4: does not practice medicine; no other-patient questions.
    assert "does not practice medicine" in lowered or "not practice medicine" in lowered
    assert "other" in lowered and "patient" in lowered


async def test_loop_injects_the_static_system_prompt_verbatim() -> None:
    llm = ScriptedLLM([final("I reviewed the labs.")])
    await build_loop(llm, make_registry()).run("Catch me up.")
    assert llm.calls[0].system == loop_mod().SYSTEM_PROMPT


# ==========================================================================
# Criterion 10 — refusal is a distinct terminal branch; partial discarded
# ==========================================================================


async def test_refusal_is_a_terminal_fallback() -> None:
    llm = ScriptedLLM([refusal()])
    result = await build_loop(llm, make_registry()).run("What should I prescribe?")
    assert result.is_fallback
    assert result.fallback.reason == loop_mod().FallbackReason.REFUSAL
    assert llm.call_count == 1  # terminal — did not continue


async def test_refusal_partial_content_is_discarded_not_rendered() -> None:
    secret = "SECRET-PARTIAL-DIAGNOSIS-DO-NOT-SHOW"
    llm = ScriptedLLM([refusal(text=f"{secret} ...")])
    result = await build_loop(llm, make_registry()).run("Diagnose her.")

    assert result.is_fallback
    assert secret not in result.output_text
    assert secret not in result.fallback.detail
    assert result.verdict is None


async def test_max_tokens_is_terminal_and_discards_partial_content() -> None:
    secret = "TRUNCATED-PARTIAL-ANSWER"
    llm = ScriptedLLM([max_tokens(text=f"{secret} ...")])
    result = await build_loop(llm, make_registry()).run("Long question.")

    assert result.is_fallback
    assert secret not in result.output_text
    assert llm.call_count == 1


# ==========================================================================
# Criterion 11 — static system prompt; patient_id only in the user turn
# ==========================================================================


async def test_system_prompt_contains_no_patient_identifier() -> None:
    pid = "pat-SECRET-4242"
    llm = ScriptedLLM([final("I reviewed the labs.")])
    await build_loop(llm, make_registry(), patient_id=pid).run("Catch me up.")

    system = llm.calls[0].system
    assert pid not in system
    # The bound patient_id is supplied in the FIRST user message instead.
    first_user = next(m for m in llm.calls[0].messages if m.role == "user")
    assert pid in first_user.content


def test_system_prompt_constant_has_no_dates_or_ids() -> None:
    import re

    prompt = loop_mod().SYSTEM_PROMPT
    # No ISO date and no obvious patient-id token baked into the static prompt.
    assert re.search(r"\d{4}-\d{2}-\d{2}", prompt) is None
    assert "pat-" not in prompt.lower()


# ==========================================================================
# Import purity — the loop pulls no anthropic / httpx (clean subprocess)
# ==========================================================================


def test_loop_module_imports_no_anthropic_or_httpx() -> None:
    code = (
        "import sys\n"
        "import copilot.agent.loop\n"
        "bad = [m for m in ('anthropic', 'httpx') if m in sys.modules]\n"
        "assert not bad, 'leaked: ' + repr(bad)\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "OK" in result.stdout


def test_ports_and_transcript_modules_are_also_pure() -> None:
    code = (
        "import sys\n"
        "import copilot.agent.ports, copilot.agent.transcript, copilot.agent.tools\n"
        "bad = [m for m in ('anthropic', 'httpx') if m in sys.modules]\n"
        "assert not bad, 'leaked: ' + repr(bad)\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


# ==========================================================================
# SDK adapter — constructor-tested only; implements the port
# ==========================================================================


def test_anthropic_adapter_constructs_and_implements_the_port() -> None:
    from copilot.agent import ports
    from copilot.llm.anthropic_client import AnthropicLLMClient

    client = AnthropicLLMClient(model="claude-opus-4-8", api_key="test-key")
    assert isinstance(client, ports.LLMClient)
    assert client.model == "claude-opus-4-8"


def test_adapter_uses_the_injected_model_id_no_hardcoded_default() -> None:
    from copilot.llm.anthropic_client import AnthropicLLMClient

    client = AnthropicLLMClient(model="claude-opus-4-8", api_key="test-key")
    # Exact string, no date suffix (design decision).
    assert client.model == "claude-opus-4-8"
    assert client.model.count("-") == 3
