"""Structured transcript of an agent run, keyed by correlation ID (T010).

Every LLM turn and every tool turn is recorded as one frozen entry — role,
name, timing, and token counts when the LLM provides them. This is the raw
material T013/T014 will ship to the trace backend; T010 only produces it.

Pure module: no ``anthropic``/``httpx`` imports.
"""

from __future__ import annotations

from enum import Enum

from pydantic import AwareDatetime, Field

from copilot.contracts.base import ContractModel


class TranscriptEntryKind(str, Enum):
    """Whether an entry records an LLM turn or a tool turn."""

    LLM = "llm"
    TOOL = "tool"


class TranscriptEntry(ContractModel):
    """One recorded turn.

    ``name`` is the model id for an LLM turn, the tool name for a tool turn.
    ``input_tokens``/``output_tokens`` are present only when the LLM reported
    them. ``detail`` carries a short machine-readable note (stop reason, error
    class) for observability.
    """

    kind: TranscriptEntryKind
    name: str = Field(min_length=1)
    started_at: AwareDatetime
    duration_seconds: float = Field(ge=0)
    input_tokens: int | None = None
    output_tokens: int | None = None
    detail: str | None = None


class Transcript(ContractModel):
    """The ordered entries of one run, keyed by its correlation ID."""

    correlation_id: str = Field(min_length=1)
    entries: tuple[TranscriptEntry, ...] = ()
