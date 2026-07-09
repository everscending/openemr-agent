"""The agent loop's tool abstraction — a small, static registry (T010).

A :class:`Tool` binds a name and description to its T002 input contract and an
async executor. The loop validates the LLM's arguments against ``input_model``
(after forcing the bound ``patient_id``), then calls ``executor`` with the
validated input. The concrete tools (which pull the FHIR/httpx client) are
wired in :mod:`copilot.agent.registry`; this module holds only the abstraction,
so it — and the loop that imports it — stay free of ``anthropic``/``httpx``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from copilot.agent.ports import ToolSchema
from copilot.contracts.base import ContractModel
from copilot.contracts.tools import ToolInput

#: An executor takes a validated T002 input and returns a T002 output model.
ToolExecutor = Callable[[ToolInput], Awaitable[ContractModel]]


@dataclass(frozen=True, slots=True)
class Tool:
    """One registered tool: its name, description, input contract, executor."""

    name: str
    description: str
    input_model: type[ToolInput]
    executor: ToolExecutor


class ToolRegistry:
    """The loop's fixed, read-only tool set (ARCHITECTURE.md §2/§9).

    Small and static by design: the tool set never changes mid-run, which is
    what keeps prompt-cache prefixes stable and injected instructions unable to
    escalate into new actions (§4 prompt-injection posture).
    """

    def __init__(self, tools: Sequence[Tool]) -> None:
        self._tools: tuple[Tool, ...] = tuple(tools)
        self._by_name: dict[str, Tool] = {t.name: t for t in self._tools}

    def get(self, name: str) -> Tool | None:
        """Return the tool with ``name``, or ``None`` if unknown."""
        return self._by_name.get(name)

    @property
    def tools(self) -> tuple[Tool, ...]:
        return self._tools

    def schemas(self) -> tuple[ToolSchema, ...]:
        """The per-tool schemas advertised to the LLM each turn."""
        return tuple(
            ToolSchema(
                name=t.name,
                description=t.description,
                input_schema=t.input_model.model_json_schema(),
            )
            for t in self._tools
        )
