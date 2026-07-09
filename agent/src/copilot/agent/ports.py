"""LLM port and wire-neutral turn shapes for the agent loop (T010).

The loop depends only on the :class:`LLMClient` protocol and the frozen value
objects here — never on a vendor SDK. Anthropic-style tool-use shapes are a
convenient template, but the interface is ours so the loop stays portable and
the whole suite runs against a scripted fake with no network (ARCHITECTURE.md
§9). The single SDK adapter (:mod:`copilot.llm.anthropic_client`) implements
this protocol; the loop imports the protocol, never the adapter.

This module imports no ``anthropic`` and no ``httpx`` — verified structurally
in a clean subprocess by the T010 suite.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Protocol, Sequence, runtime_checkable

from pydantic import Field

from copilot.contracts.base import ContractModel


class StopReason(str, Enum):
    """Why the LLM stopped — a closed set the loop matches exhaustively.

    ``refusal`` and ``max_tokens`` are terminal branches, **not** exceptions: a
    refusal is a successful HTTP 200 with empty or partial content, and a
    max-tokens stop is a truncated response. Reading content unconditionally
    would crash on the first; both are handled as control flow (criterion 10).
    """

    TOOL_USE = "tool_use"
    END_TURN = "end_turn"
    REFUSAL = "refusal"
    MAX_TOKENS = "max_tokens"


class ToolCallRequest(ContractModel):
    """One tool the LLM asked the loop to run, with its raw arguments.

    ``arguments`` is whatever the model produced; the loop overrides
    ``patient_id`` with the bound chart's id before validating (criterion 6),
    so a request for another patient's data can never be executed as-is.
    """

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolSchema(ContractModel):
    """A single tool's advertised schema, sent to the LLM each turn."""

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    input_schema: dict[str, Any]


class LLMMessage(ContractModel):
    """One conversation turn the loop maintains and replays to the LLM.

    ``tool_calls`` carries the assistant's requested calls; ``tool_call_id``
    links a tool result back to its call; ``is_error`` marks a tool result the
    loop fed back after a validation or execution failure (criteria 3/7).
    """

    role: str = Field(min_length=1)
    content: str
    tool_call_id: str | None = None
    tool_calls: tuple[ToolCallRequest, ...] = ()
    is_error: bool = False


class LLMResponse(ContractModel):
    """One LLM turn: a stop reason plus whatever content it carries.

    ``text`` is the draft on ``end_turn`` (and may be partial on ``refusal`` /
    ``max_tokens`` — the loop discards it there). ``tool_calls`` is populated on
    ``tool_use``. Token counts are echoed into the transcript when provided.
    """

    stop_reason: StopReason
    text: str | None = None
    tool_calls: tuple[ToolCallRequest, ...] = ()
    input_tokens: int | None = None
    output_tokens: int | None = None


@runtime_checkable
class LLMClient(Protocol):
    """The one seam between the loop and any LLM.

    A single async method: given the static system prompt, the running
    conversation, and the tool registry's schemas, return one turn. Every test
    drives a scripted fake implementing exactly this.
    """

    async def complete(
        self,
        *,
        system: str,
        messages: Sequence[LLMMessage],
        tools: Sequence[ToolSchema],
    ) -> LLMResponse: ...
