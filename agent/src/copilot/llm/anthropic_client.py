"""The single Anthropic SDK adapter implementing :class:`LLMClient` (T010).

This is the *only* module that imports the ``anthropic`` SDK. The loop imports
the port (:mod:`copilot.agent.ports`), never this adapter — so importing the
loop pulls no SDK into ``sys.modules`` (the same containment used for OTel, §7,
§9). Behavioural tests drive a scripted fake; this adapter is constructor-tested
only (no network in the suite).

Design decisions (ARCHITECTURE.md §9, do not relitigate):

* Model id is ``claude-opus-4-8`` — exact string, no date suffix — and is
  config-injected, never hardcoded at a call site.
* No sampling parameters (``temperature``/``top_p``/``top_k``) and no
  ``budget_tokens``: current Claude models reject all of them with a 400.
* ``refusal`` and ``max_tokens`` are stop-reason *branches*, mapped to the
  port's :class:`StopReason` — never exceptions, never an unconditional
  ``content[0]`` read.
"""

from __future__ import annotations

from typing import Any, Sequence

import anthropic

from copilot.agent.ports import (
    LLMMessage,
    LLMResponse,
    StopReason,
    ToolCallRequest,
    ToolSchema,
)

DEFAULT_MAX_TOKENS = 8192

_STOP_REASONS: dict[str, StopReason] = {
    "tool_use": StopReason.TOOL_USE,
    "max_tokens": StopReason.MAX_TOKENS,
    "refusal": StopReason.REFUSAL,
    "end_turn": StopReason.END_TURN,
    "stop_sequence": StopReason.END_TURN,
    "pause_turn": StopReason.END_TURN,
}


class AnthropicLLMClient:
    """Adapts the async Anthropic Messages API to the ``LLMClient`` port."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        self._model = model
        self._max_tokens = max_tokens
        # Constructing the SDK client is lazy — it opens no connection here.
        self._client = client or anthropic.AsyncAnthropic(api_key=api_key)

    @property
    def model(self) -> str:
        return self._model

    async def complete(
        self,
        *,
        system: str,
        messages: Sequence[LLMMessage],
        tools: Sequence[ToolSchema],
    ) -> LLMResponse:
        response = await self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=system,
            messages=[self._to_wire(m) for m in messages],
            tools=[self._tool_to_wire(t) for t in tools],
        )
        return self._from_wire(response)

    # -- request mapping ---------------------------------------------------

    @staticmethod
    def _tool_to_wire(tool: ToolSchema) -> dict[str, Any]:
        return {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_schema,
        }

    @staticmethod
    def _to_wire(message: LLMMessage) -> dict[str, Any]:
        if message.role == "tool":
            return {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": message.tool_call_id,
                        "content": message.content,
                        "is_error": message.is_error,
                    }
                ],
            }
        if message.role == "assistant" and message.tool_calls:
            content: list[dict[str, Any]] = []
            if message.content:
                content.append({"type": "text", "text": message.content})
            for call in message.tool_calls:
                content.append(
                    {
                        "type": "tool_use",
                        "id": call.id,
                        "name": call.name,
                        "input": dict(call.arguments),
                    }
                )
            return {"role": "assistant", "content": content}
        return {"role": message.role, "content": message.content}

    # -- response mapping --------------------------------------------------

    def _from_wire(self, response: Any) -> LLMResponse:
        stop_reason = _STOP_REASONS.get(
            response.stop_reason or "end_turn", StopReason.END_TURN
        )
        usage = getattr(response, "usage", None)
        input_tokens = getattr(usage, "input_tokens", None)
        output_tokens = getattr(usage, "output_tokens", None)

        # Never read content unconditionally: a refusal is a 200 with empty or
        # partial content, and the loop discards it.
        if stop_reason is StopReason.REFUSAL:
            return LLMResponse(
                stop_reason=stop_reason,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )

        text_parts: list[str] = []
        tool_calls: list[ToolCallRequest] = []
        for block in getattr(response, "content", []) or []:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                text_parts.append(getattr(block, "text", ""))
            elif block_type == "tool_use":
                raw_input = getattr(block, "input", {}) or {}
                tool_calls.append(
                    ToolCallRequest(
                        id=getattr(block, "id", ""),
                        name=getattr(block, "name", ""),
                        arguments=dict(raw_input),
                    )
                )

        return LLMResponse(
            stop_reason=stop_reason,
            text="".join(text_parts) if text_parts else None,
            tool_calls=tuple(tool_calls),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
