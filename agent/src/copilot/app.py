"""FastAPI application factory for the Clinical Co-Pilot agent service.

Includes ``/chat`` (T011): a conversation-scoped endpoint over the T010 agent
loop. Streaming is **buffered**: T008/T009 verification runs to completion on
the full draft before any claim-bearing byte is emitted. Token-by-token
passthrough would surface an unverified claim on the wire and then retract
it — strictly worse than the added latency. This trades against the §5
first-token target (<3s): a buffered ``/chat`` reply cannot beat that target
the way an unbuffered token stream could, and that trade is deliberate. The
SSE stream still emits promptly: a ``meta`` event (conversation_id,
correlation_id) immediately, then the verified ``message`` event, then a
terminal ``verdict`` event carrying the T008/T009 counts.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timezone
from typing import Mapping

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import JSONResponse, StreamingResponse

from copilot.agent.loop import AgentLoop
from copilot.agent.ports import LLMClient, LLMMessage
from copilot.agent.tools import ToolRegistry
from copilot.contracts.chat import ChatRequest, ChatResponse
from copilot.contracts.verification import VerificationCounts
from copilot.conversation import (
    ConversationRecord,
    ConversationStore,
    InMemoryConversationStore,
    TurnRole,
    hash_token,
)
from copilot.correlation import (
    CorrelationIdMiddleware,
    get_correlation_id,
    install_correlation_log_record_factory,
)
from copilot.readiness import (
    Checker,
    default_checkers,
    default_deadline,
    run_readiness_checks,
)

DEFAULT_CHAT_MODEL = "claude-opus-4-8"
DEFAULT_CHAT_MAX_STEPS = 6

#: Every scope-mismatch outcome (unknown id, expired, wrong patient, wrong
#: user) raises this exact ``HTTPException`` — same status, same body — so a
#: caller who does not own a conversation cannot distinguish "never existed"
#: from "exists but isn't yours" (an existence oracle over PHI).
_CONVERSATION_NOT_FOUND_DETAIL = "conversation not found"


def _conversation_not_found() -> HTTPException:
    return HTTPException(status_code=404, detail=_CONVERSATION_NOT_FOUND_DETAIL)


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


def _default_chat_llm() -> LLMClient:
    # Imported lazily: constructing the SDK client opens no connection, but
    # keeping ``anthropic`` out of this module's top-level import list mirrors
    # the import-purity discipline the loop enforces for itself (T010).
    from copilot.llm.anthropic_client import AnthropicLLMClient

    return AnthropicLLMClient(model=DEFAULT_CHAT_MODEL)


def _default_chat_registry_factory() -> Callable[[str], ToolRegistry]:
    def factory(token: str) -> ToolRegistry:
        from copilot.agent.registry import build_default_registry
        from copilot.fhir import FhirClient
        from copilot.readiness import OPENEMR_FHIR_BASE_URL_ENV

        base_url = os.environ.get(OPENEMR_FHIR_BASE_URL_ENV)
        if not base_url:
            raise RuntimeError(
                f"{OPENEMR_FHIR_BASE_URL_ENV} is not configured"
            )
        return build_default_registry(FhirClient(base_url, token=token))

    return factory


def _sse_frame(event: str, data: dict[str, object]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode("utf-8")


async def _sse_stream(payload: ChatResponse) -> AsyncIterator[bytes]:
    """The buffered SSE frame sequence: meta, then verified text, then verdict."""
    yield _sse_frame(
        "meta",
        {
            "conversation_id": payload.conversation_id,
            "correlation_id": payload.correlation_id,
        },
    )
    yield _sse_frame("message", {"reply": payload.reply})
    yield _sse_frame(
        "verdict",
        {
            "verification": payload.verification.model_dump(mode="json"),
            "fallback": payload.fallback,
        },
    )


def create_app(
    readiness_checkers: Mapping[str, Checker] | None = None,
    readiness_deadline: float | None = None,
    *,
    chat_llm: LLMClient | None = None,
    chat_registry_factory: Callable[[str], ToolRegistry] | None = None,
    chat_model: str = DEFAULT_CHAT_MODEL,
    chat_max_steps: int = DEFAULT_CHAT_MAX_STEPS,
    conversation_store: ConversationStore | None = None,
    conversation_ttl_seconds: float = 2 * 60 * 60,
    clock: Callable[[], datetime] = _default_clock,
) -> FastAPI:
    """Build and return the Co-Pilot FastAPI application.

    ``readiness_checkers`` maps dependency keys to async checkers (return on
    success, raise on failure) probed by ``/ready``; omitted, the production
    reachability checkers are used. ``readiness_deadline`` bounds the overall
    probe time in seconds (default 5s, env-overridable via
    ``READINESS_DEADLINE_SECONDS``).

    ``chat_llm`` / ``chat_registry_factory`` back ``/chat`` (T011); omitted,
    production defaults are used (a single Anthropic client shared across
    requests, and a per-request tool registry built from the caller's own
    bearer token — §4's OAuth-per-request boundary). Tests inject a scripted
    fake for both, so the suite makes no network call. ``conversation_store``
    is the Redis-shaped state backend; omitted, an in-process store is built
    from ``conversation_ttl_seconds`` (default 2h, ARCHITECTURE.md §7) and
    ``clock`` (an injected clock, for deterministic TTL tests).
    """
    install_correlation_log_record_factory()

    checkers: Mapping[str, Checker] = (
        readiness_checkers if readiness_checkers is not None else default_checkers()
    )
    deadline = readiness_deadline if readiness_deadline is not None else default_deadline()

    resolved_llm: LLMClient = chat_llm if chat_llm is not None else _default_chat_llm()
    resolved_registry_factory: Callable[[str], ToolRegistry] = (
        chat_registry_factory
        if chat_registry_factory is not None
        else _default_chat_registry_factory()
    )
    store: ConversationStore = (
        conversation_store
        if conversation_store is not None
        else InMemoryConversationStore(ttl_seconds=conversation_ttl_seconds, now=clock)
    )

    app = FastAPI(title="Clinical Co-Pilot Agent")
    app.add_middleware(CorrelationIdMiddleware)

    @app.get("/health")
    def health() -> dict[str, str]:
        """Liveness probe: no external calls, process-up check only."""
        return {"status": "ok"}

    @app.get("/ready")
    async def ready() -> JSONResponse:
        """Readiness probe: concurrently checks all configured dependencies."""
        status_code, body = await run_readiness_checks(checkers, deadline=deadline)
        return JSONResponse(status_code=status_code, content=body)

    @app.post("/chat")
    async def chat(chat_request: ChatRequest, stream: bool = False) -> Response:
        """One conversational turn, scoped to (patient, caller) and TTL-bound.

        ``conversation_id`` omitted starts a new conversation. Supplied, it
        must resolve to a conversation bound to the *same* ``patient_id`` and
        the same caller (by token hash); any mismatch — unknown id, expired,
        wrong patient, wrong user — returns the identical 404 (never 403: a
        403 would confirm existence to a caller who does not own the
        conversation). ``stream=true`` returns the buffered SSE sequence
        (meta, verified message, verdict); otherwise a single JSON body.
        """
        correlation_id = get_correlation_id() or str(uuid.uuid4())
        token_hash = hash_token(chat_request.token)

        if chat_request.conversation_id is None:
            conversation_id = str(uuid.uuid4())
            record = ConversationRecord(
                patient_id=chat_request.patient_id,
                user_token_hash=token_hash,
            )
        else:
            conversation_id = chat_request.conversation_id
            existing = store.get(conversation_id)
            if (
                existing is None
                or existing.patient_id != chat_request.patient_id
                or existing.user_token_hash != token_hash
            ):
                raise _conversation_not_found()
            record = existing

        history = tuple(
            LLMMessage(role=turn.role.value, content=turn.content)
            for turn in record.turns
        )

        # Patient binding, one layer up: the loop is constructed with the
        # conversation's *bound* patient_id (record.patient_id), never the
        # request's claimed one — the mismatch path above already rejected
        # any request whose claimed patient_id disagrees with the binding.
        agent_loop = AgentLoop(
            llm=resolved_llm,
            registry=resolved_registry_factory(chat_request.token),
            patient_id=record.patient_id,
            model=chat_model,
            correlation_id=correlation_id,
            max_steps=chat_max_steps,
        )
        result = await agent_loop.run(chat_request.message, history=history)

        updated = record.with_turn(TurnRole.USER, chat_request.message).with_turn(
            TurnRole.ASSISTANT, result.output_text
        )
        store.put(conversation_id, updated)

        counts = (
            result.verdict.counts
            if result.verdict is not None
            else VerificationCounts(claims_total=0, claims_passed=0, claims_stripped=0)
        )
        payload = ChatResponse(
            conversation_id=conversation_id,
            correlation_id=correlation_id,
            reply=result.output_text,
            verification=counts,
            fallback=result.is_fallback,
        )

        if stream:
            return StreamingResponse(
                _sse_stream(payload), media_type="text/event-stream"
            )
        return JSONResponse(status_code=200, content=payload.model_dump(mode="json"))

    return app


app = create_app()
