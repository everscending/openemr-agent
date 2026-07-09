"""Agent tool-use loop (T010).

The loop, its ports, its transcript, and its tool abstraction are all pure —
they import no ``anthropic`` and no ``httpx``. The concrete tool wiring lives in
:mod:`copilot.agent.registry` and the SDK adapter in
:mod:`copilot.llm.anthropic_client`; neither is imported here, so importing this
package (or :mod:`copilot.agent.loop`) never pulls a vendor SDK or HTTP client
into ``sys.modules``.
"""

from copilot.agent.loop import (
    FALLBACK_TEXT,
    SYSTEM_PROMPT,
    AgentLoop,
    AgentResult,
    FallbackReason,
    FallbackRequired,
    collect_refs,
)
from copilot.agent.ports import (
    LLMClient,
    LLMMessage,
    LLMResponse,
    StopReason,
    ToolCallRequest,
    ToolSchema,
)
from copilot.agent.tools import Tool, ToolRegistry
from copilot.agent.transcript import (
    Transcript,
    TranscriptEntry,
    TranscriptEntryKind,
)

__all__ = [
    "AgentLoop",
    "AgentResult",
    "FALLBACK_TEXT",
    "FallbackReason",
    "FallbackRequired",
    "LLMClient",
    "LLMMessage",
    "LLMResponse",
    "SYSTEM_PROMPT",
    "StopReason",
    "Tool",
    "ToolCallRequest",
    "ToolRegistry",
    "ToolSchema",
    "Transcript",
    "TranscriptEntry",
    "TranscriptEntryKind",
    "collect_refs",
]
