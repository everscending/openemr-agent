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

from copilot.agent.loop import DEFAULT_LLM_TIMEOUT_SECONDS, AgentLoop, AgentResult
from copilot.agent.ports import LLMClient, LLMMessage
from copilot.agent.tools import ToolRegistry
from copilot.contracts.chat import ChatRequest, ChatResponse, DegradedReason
from copilot.contracts.tools import GetPatientSnapshotInput, PatientSnapshotOutput
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

#: Rendered as the reply text when the LLM is down. Generic and PHI-free — never
#: an exception message or stack trace (T012, ARCHITECTURE.md §7).
_DEGRADED_FIRST_TURN_REPLY = (
    "The co-pilot is temporarily unavailable. A structured snapshot of the "
    "chart is shown below, taken straight from the record."
)
_DEGRADED_FOLLOW_UP_REPLY = (
    "The co-pilot is temporarily unavailable. The chart snapshot remains "
    "viewable; please try again shortly."
)
_ZERO_COUNTS = VerificationCounts(
    claims_total=0, claims_passed=0, claims_stripped=0
)

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
    message_data: dict[str, object] = {"reply": payload.reply}
    if payload.snapshot is not None:
        message_data["snapshot"] = payload.snapshot.model_dump(mode="json")
    yield _sse_frame("message", message_data)
    verdict_data: dict[str, object] = {
        "verification": payload.verification.model_dump(mode="json"),
        "fallback": payload.fallback,
    }
    if payload.degraded is not None:
        verdict_data["degraded"] = payload.degraded.value
    yield _sse_frame("verdict", verdict_data)


async def _fetch_snapshot_no_llm(
    registry: ToolRegistry, patient_id: str
) -> PatientSnapshotOutput | None:
    """Fetch the T005 snapshot directly, with no model involvement (criterion 1).

    Used only on the *first* turn of a conversation when the LLM was down before
    any tool ran, so no snapshot was captured. Returns ``None`` if the registry
    has no snapshot tool (e.g. a test registry) — the degraded marker still ships.
    """
    tool = registry.get("get_patient_snapshot")
    if tool is None:
        return None
    validated = tool.input_model(patient_id=patient_id)
    result = await tool.executor(validated)
    return result if isinstance(result, PatientSnapshotOutput) else None


async def _build_degraded_payload(
    *,
    conversation_id: str,
    correlation_id: str,
    record: ConversationRecord,
    registry: ToolRegistry,
    result: AgentResult,
) -> ChatResponse:
    """Assemble the ``degraded: llm_unavailable`` response (T012).

    The snapshot is rendered whenever the conversation has **zero stored
    turns** — reusing one the loop already captured (criterion 3), else
    fetched directly with no LLM (criterion 1). This is conversation *state*,
    not request shape: a retry against the same id while the conversation
    still has no successful turns has no prior context to fall back on
    either, so it is semantically still a first turn and must render the
    snapshot again — a clinician who retries during an outage must not watch
    the med list vanish. Only once a turn has actually succeeded does a later
    outage become a real follow-up with prior context to point back to, and
    only then does criterion 4's "no meaningful non-AI rendering" apply and
    the snapshot drop.
    """
    if not record.turns:
        snapshot = result.snapshot
        if snapshot is None:
            snapshot = await _fetch_snapshot_no_llm(registry, record.patient_id)
        reply = _DEGRADED_FIRST_TURN_REPLY
    else:
        snapshot = None
        reply = _DEGRADED_FOLLOW_UP_REPLY

    # Structured tool data is never run through verify_response: there are no
    # model claims to ground, so counts are all zero and nothing is stripped.
    return ChatResponse(
        conversation_id=conversation_id,
        correlation_id=correlation_id,
        reply=reply,
        verification=_ZERO_COUNTS,
        fallback=False,
        degraded=DegradedReason.LLM_UNAVAILABLE,
        snapshot=snapshot,
    )


def create_app(
    readiness_checkers: Mapping[str, Checker] | None = None,
    readiness_deadline: float | None = None,
    *,
    chat_llm: LLMClient | None = None,
    chat_registry_factory: Callable[[str], ToolRegistry] | None = None,
    chat_model: str = DEFAULT_CHAT_MODEL,
    chat_max_steps: int = DEFAULT_CHAT_MAX_STEPS,
    chat_llm_timeout: float = DEFAULT_LLM_TIMEOUT_SECONDS,
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
            # Bind the conversation (patient_id + token hash) immediately, with
            # zero turns. This must happen before the LLM is ever called: a
            # degraded first turn (T012) must not hand the caller a phantom
            # conversation_id that resolves to nothing once the LLM recovers —
            # the binding is what lets that follow-up succeed, and it is live
            # from creation regardless of what the LLM does next.
            store.put(conversation_id, record)
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
        registry = resolved_registry_factory(chat_request.token)
        agent_loop = AgentLoop(
            llm=resolved_llm,
            registry=registry,
            patient_id=record.patient_id,
            model=chat_model,
            correlation_id=correlation_id,
            max_steps=chat_max_steps,
            llm_timeout=chat_llm_timeout,
        )
        result = await agent_loop.run(chat_request.message, history=history)

        if result.is_degraded:
            payload = await _build_degraded_payload(
                conversation_id=conversation_id,
                correlation_id=correlation_id,
                record=record,
                registry=registry,
                result=result,
            )
            # The failed turn is not appended: no assistant reply exists, so
            # nothing is recorded and prior turns stay byte-identical (a
            # brand-new conversation keeps the zero-turn record it was bound
            # with above). The conversation remains usable by its owner once
            # the LLM returns.
        else:
            updated = record.with_turn(
                TurnRole.USER, chat_request.message
            ).with_turn(TurnRole.ASSISTANT, result.output_text)
            store.put(conversation_id, updated)
            payload = ChatResponse(
                conversation_id=conversation_id,
                correlation_id=correlation_id,
                reply=result.output_text,
                verification=(
                    result.verdict.counts
                    if result.verdict is not None
                    else _ZERO_COUNTS
                ),
                fallback=result.is_fallback,
            )

        if stream:
            return StreamingResponse(
                _sse_stream(payload), media_type="text/event-stream"
            )
        return JSONResponse(
            status_code=200,
            content=payload.model_dump(mode="json", exclude_none=True),
        )

    return app


app = create_app()
