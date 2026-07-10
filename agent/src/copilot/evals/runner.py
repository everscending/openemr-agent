"""Execute one eval case end-to-end against the in-process app (T015).

Reuses the real production seams rather than reimplementing a harness
(ARCHITECTURE.md section 8): the same T010 ``AgentLoop`` engine
``copilot.app``'s ``/chat`` route delegates to, the same
``build_default_registry`` tool wiring, the same ``FhirClient`` — with a
mock transport standing in for the network and a scripted fake standing in
for the LLM (T010's ``LLMClient`` port). No live model call, no live FHIR
call, ever.

Deliberately does **not** import :mod:`copilot.app`: that module's
module-level ``app = create_app()`` unconditionally constructs the default
Anthropic client at import time (there is no way to import ``copilot.app``
without triggering it), which would violate this ticket's own "importing the
eval runner pulls no anthropic" requirement. Driving ``AgentLoop`` directly
— exactly what T012's own tests do (``build_loop`` in
``tests/test_llm_fallback.py``) — is both the more portable reuse and the one
that keeps this package's import graph anthropic-free.

The tool registry is wrapped so every tool result actually produced during
the run is recorded (name + real output object, or the failure that
occurred) — this is what lets checks assert on real per-category coverage
and real medication-reconciliation output without re-deriving them from the
case's own fixture data.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from copilot.agent import ports
from copilot.agent.loop import AgentLoop
from copilot.agent.ports import LLMMessage
from copilot.agent.registry import build_default_registry
from copilot.agent.tools import Tool, ToolRegistry
from copilot.evals.checks import CHECK_REGISTRY
from copilot.evals.fhir_mock import build_fhir_mock_transport
from copilot.evals.results import CaseReport, RecordedCall, RunResponse
from copilot.evals.schema import EvalCase, ScriptedResponse
from copilot.fhir import FhirClient

#: Never a real endpoint — every request is served by the mock transport.
EVAL_BASE_URL = "https://eval.invalid/apis/default/fhir"
EVAL_TOKEN = "eval-harness-token"  # noqa: S105 - not a credential, a fixture constant
EVAL_MODEL = "eval-harness-model"


class ScriptedEvalLLM:
    """A scripted fake implementing T010's ``LLMClient`` port for one case.

    Returns the case's ``llm_script`` items in order across the *whole* run
    (every internal tool-use step of every simulated ``/chat`` turn draws
    from the same flat queue). Records every item actually consumed, and
    every call's arguments, so checks/tests can assert on what the scenario
    really reached (e.g. that the last turn consumed was a refusal, or that a
    later turn's messages replay an earlier turn's history).
    """

    def __init__(self, script: Sequence[ports.LLMResponse]) -> None:
        self._script: list[ports.LLMResponse] = list(script)
        self.consumed: list[ports.LLMResponse] = []
        self.calls: list[Sequence[LLMMessage]] = []

    async def complete(
        self, *, system: str, messages: Sequence[LLMMessage], tools: Any
    ) -> ports.LLMResponse:
        self.calls.append(tuple(messages))
        if not self._script:
            raise AssertionError(
                "eval case's llm_script exhausted: the loop called the LLM "
                "more times than the case scripted"
            )
        item = self._script.pop(0)
        self.consumed.append(item)
        return item


def _to_llm_response(item: ScriptedResponse) -> ports.LLMResponse:
    return ports.LLMResponse(
        stop_reason=ports.StopReason(item.stop_reason),
        text=item.text,
        tool_calls=tuple(
            ports.ToolCallRequest(id=tc.id, name=tc.name, arguments=dict(tc.arguments))
            for tc in item.tool_calls
        ),
    )


def _recording_registry(
    inner: ToolRegistry, sink: list[RecordedCall]
) -> ToolRegistry:
    """Wrap every tool's executor to record its real result or failure."""

    def _wrap(tool: Tool) -> Tool:
        async def executor(validated: object, _tool: Tool = tool) -> object:
            try:
                result = await _tool.executor(validated)  # type: ignore[arg-type]
            except Exception as exc:
                sink.append(
                    RecordedCall(
                        tool_name=_tool.name,
                        result=None,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
                raise
            sink.append(
                RecordedCall(tool_name=_tool.name, result=result, error=None)
            )
            return result

        return Tool(
            name=tool.name,
            description=tool.description,
            input_model=tool.input_model,
            executor=executor,  # type: ignore[arg-type]
        )

    return ToolRegistry([_wrap(tool) for tool in inner.tools])


async def execute_case(case: EvalCase) -> RunResponse:
    """Run ``case`` end-to-end and return everything a check may inspect.

    Mirrors ``copilot.app``'s ``/chat`` history-replay rule exactly: a turn's
    user/assistant messages are appended to history for the *next* turn only
    when this turn produced a real answer (never a fallback or degraded
    turn) — so a scripted multi-turn scenario behaves exactly as a real
    conversation would.
    """
    recorded: list[RecordedCall] = []
    transport = build_fhir_mock_transport(
        case.scenario.fhir_fixture, failing_resource_types=case.scenario.fhir_failures
    )
    fhir_client = FhirClient(EVAL_BASE_URL, EVAL_TOKEN, transport=transport)
    base_registry = build_default_registry(fhir_client)
    registry = _recording_registry(base_registry, recorded)
    llm = ScriptedEvalLLM([_to_llm_response(item) for item in case.scenario.llm_script])

    loop = AgentLoop(
        llm=llm,
        registry=registry,
        patient_id=case.scenario.patient_id,
        model=EVAL_MODEL,
        correlation_id=f"eval-{case.id}",
    )

    history: list[LLMMessage] = []
    turns = []
    for message in case.scenario.messages:
        result = await loop.run(message, history=tuple(history))
        turns.append(result)
        if not result.is_fallback and not result.is_degraded:
            history.append(LLMMessage(role="user", content=message))
            history.append(LLMMessage(role="assistant", content=result.output_text))

    return RunResponse(
        turns=tuple(turns),
        recorded_calls=tuple(recorded),
        llm_consumed=tuple(llm.consumed),
        llm_calls=tuple(llm.calls),
    )


async def run_case(case: EvalCase) -> CaseReport:
    """Execute ``case`` and evaluate every one of its named checks."""
    response = await execute_case(case)
    outcomes = [
        CHECK_REGISTRY[check.name](case, response, check.params)
        for check in case.checks
    ]
    passed = all(outcome.passed for outcome in outcomes)
    reasons = tuple(
        f"{outcome.check}: {outcome.detail}" for outcome in outcomes if not outcome.passed
    )
    return CaseReport(
        id=case.id, guards_against=case.guards_against, passed=passed, reasons=reasons
    )


async def run_cases(cases: Sequence[EvalCase]) -> tuple[CaseReport, ...]:
    """Run every case in order, returning one report per case."""
    reports = []
    for case in cases:
        reports.append(await run_case(case))
    return tuple(reports)
