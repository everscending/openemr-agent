"""Runner-observed response shapes and the per-case report (T015).

``RunResponse`` bundles the real ``AgentResult`` produced by each scripted
``/chat``-equivalent turn (T010's ``AgentLoop`` — the same engine
``copilot.app``'s ``/chat`` route delegates to) plus every tool result the
run actually produced (via a recording tool registry) and every scripted LLM
turn actually consumed. Checks assert on these — real, observed behavior —
never on the case's own fixture data copied back at itself.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import Field

from copilot.agent.loop import AgentResult
from copilot.agent.ports import LLMMessage, LLMResponse
from copilot.contracts.base import ContractModel
from copilot.evals.schema import GuardsAgainst


@dataclass(frozen=True, slots=True)
class RecordedCall:
    """One tool invocation actually made during the run, name + real outcome.

    ``result`` is the real tool-output model instance (e.g. a
    ``PatientSnapshotOutput``) on success; ``None`` on failure, with the
    failure's type name captured in ``error``.
    """

    tool_name: str
    result: object | None
    error: str | None


@dataclass(frozen=True, slots=True)
class RunResponse:
    """Everything a check may inspect: real, observed outcomes of one run."""

    turns: tuple[AgentResult, ...]
    recorded_calls: tuple[RecordedCall, ...]
    llm_consumed: tuple[LLMResponse, ...]
    #: Every ``LLMClient.complete`` call's ``messages`` argument, in order —
    #: across every internal tool-use step of every turn. Lets a test/check
    #: assert exactly what a later call replayed (history-carry proof).
    llm_calls: tuple[tuple[LLMMessage, ...], ...] = ()

    @property
    def last(self) -> AgentResult:
        """The final turn's ``AgentResult`` — what most checks care about."""
        return self.turns[-1]

    def tool_results(self, name: str) -> tuple[object, ...]:
        """Every successful result the named tool actually produced, in order."""
        return tuple(
            call.result
            for call in self.recorded_calls
            if call.tool_name == name and call.result is not None
        )


class CaseReport(ContractModel):
    """The machine-readable outcome of running one eval case.

    ``reasons`` is empty exactly when ``passed`` is ``True``; each failure
    reason names the failing check and what it observed (never a bare
    "failed").
    """

    id: str = Field(min_length=1)
    guards_against: GuardsAgainst
    passed: bool
    reasons: tuple[str, ...] = ()
