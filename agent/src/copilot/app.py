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
import logging
import os
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timezone
from typing import Mapping

from fastapi import BackgroundTasks, FastAPI, HTTPException, Response
from fastapi.responses import JSONResponse, StreamingResponse
from opentelemetry.trace import Tracer

from copilot.agent.loop import DEFAULT_LLM_TIMEOUT_SECONDS, AgentLoop, AgentResult
from copilot.agent.ports import LLMClient, LLMMessage
from copilot.agent.tools import ToolRegistry
from copilot.audit import (
    AuditBridgeClient,
    AuditInvocationRecord,
    AuditMetricsRecorder,
    AuditOutcome,
)
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
from copilot.telemetry.metrics import TelemetryMetrics

_logger = logging.getLogger("copilot.observability")

DEFAULT_CHAT_MODEL = "claude-sonnet-5"
DEFAULT_CHAT_MAX_STEPS = 6

#: The default chat model is env-configurable (T043, cost — Opus is ~2x
#: Sonnet 5 per-token); resolved at call time via ``default_chat_model()``,
#: mirroring ``readiness.py``'s ``default_deadline()`` shape — never baked
#: into a module-level constant via ``os.environ.get()`` at import time.
CHAT_MODEL_ENV = "CHAT_MODEL"

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

#: T013/ARCHITECTURE.md §7: the audit-bridge endpoint URL (T016, PHP side, out
#: of scope here). Unset in most environments today (the module endpoint does
#: not exist yet) — omitted, audit dispatch is simply disabled, never a
#: startup failure: fail-open extends to configuration absence too.
AUDIT_BRIDGE_URL_ENV = "AUDIT_BRIDGE_URL"


def _conversation_not_found() -> HTTPException:
    return HTTPException(status_code=404, detail=_CONVERSATION_NOT_FOUND_DETAIL)


def _noop_sequence_recorder(event: str) -> None:
    """Default no-op audit ordering hook — tests inject a real recorder."""
    return None


def default_chat_model() -> str:
    """The chat model to use when no explicit ``chat_model`` is supplied.

    Env-configurable (default ``claude-sonnet-5``, override via
    ``CHAT_MODEL``), resolved fresh on every call — never cached at import
    time.
    """
    return os.environ.get(CHAT_MODEL_ENV, DEFAULT_CHAT_MODEL)


def _default_audit_bridge(metrics: AuditMetricsRecorder) -> AuditBridgeClient | None:
    base_url = os.environ.get(AUDIT_BRIDGE_URL_ENV)
    if not base_url:
        return None
    return AuditBridgeClient(base_url=base_url, metrics=metrics)


def _default_tracer() -> Tracer:
    # Imported lazily: this is the one seam that pulls in the OTel SDK bootstrap
    # module, kept out of this module's top-level import list the same way
    # ``_default_chat_llm`` keeps ``anthropic`` out (T014's import-guard scope
    # is module-level imports only — see ``copilot.telemetry.import_guard``).
    from copilot.telemetry.bootstrap import configure_tracing

    return configure_tracing()


def _build_audit_record(
    *,
    user_token_hash: str,
    patient_id: str,
    correlation_id: str,
    conversation_id: str,
    occurred_at: datetime,
    result: AgentResult,
) -> AuditInvocationRecord:
    """The one invocation record for a completed turn (T013, criterion 1).

    ``result`` alone determines the three mutually-exclusive outcome shapes:
    T012 degraded (the LLM never answered), T010 fallback (refusal/malformed/
    tool-args/step-cap), or answered (a verdict was returned — including the
    verification layer's *own* fallback-triggered path, T008/T009: that is
    still an answered turn from the loop's perspective, distinguished only by
    its ``claims_*`` counts, never conflated with the T010 axis).
    """
    if result.is_degraded:
        return AuditInvocationRecord(
            user_token_hash=user_token_hash,
            patient_id=patient_id,
            correlation_id=correlation_id,
            conversation_id=conversation_id,
            occurred_at=occurred_at,
            claims_total=0,
            claims_passed=0,
            claims_stripped=0,
            outcome=AuditOutcome.DEGRADED,
            degraded=result.degraded,
        )
    if result.is_fallback:
        assert result.fallback is not None  # narrows for the type checker
        return AuditInvocationRecord(
            user_token_hash=user_token_hash,
            patient_id=patient_id,
            correlation_id=correlation_id,
            conversation_id=conversation_id,
            occurred_at=occurred_at,
            claims_total=0,
            claims_passed=0,
            claims_stripped=0,
            outcome=AuditOutcome.FALLBACK,
            fallback_reason=result.fallback.reason,
        )
    counts = result.verdict.counts if result.verdict is not None else _ZERO_COUNTS
    return AuditInvocationRecord(
        user_token_hash=user_token_hash,
        patient_id=patient_id,
        correlation_id=correlation_id,
        conversation_id=conversation_id,
        occurred_at=occurred_at,
        claims_total=counts.claims_total,
        claims_passed=counts.claims_passed,
        claims_stripped=counts.claims_stripped,
        outcome=AuditOutcome.ANSWERED,
    )


async def _dispatch_audit(
    audit_bridge: AuditBridgeClient,
    audit_record: AuditInvocationRecord,
    sequence_recorder: Callable[[str], None],
    user_token: str,
) -> None:
    """Background task: attempt one delivery. Never raises (T013 fail-open).

    Runs strictly after the response has been finalized — attached as a
    Starlette ``BackgroundTasks`` entry, which executes only once the
    response body has been fully sent (JSON) or the stream generator has been
    exhausted (SSE). ``sequence_recorder`` is the test-only ordering seam
    (criterion 4); production callers pass the no-op default. ``user_token`` is
    the acting user's raw bearer, forwarded to the bridge for attribution and
    binding (T016) — it never leaves this call except as the outgoing
    ``Authorization`` header.
    """
    sequence_recorder("audit_post_attempted")
    await audit_bridge.deliver(audit_record, user_token=user_token)


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


def _default_chat_llm(model: str) -> LLMClient:
    # Imported lazily: constructing the SDK client opens no connection, but
    # keeping ``anthropic`` out of this module's top-level import list mirrors
    # the import-purity discipline the loop enforces for itself (T010).
    #
    # ``model`` is the caller's already-resolved chat model (T043) — this
    # function must never independently re-resolve or hardcode a model string,
    # or the LLM actually called could silently desynchronize from the model
    # name recorded for telemetry/cost lookups (AgentLoop's ``model=`` below).
    from copilot.llm.anthropic_client import AnthropicLLMClient

    return AnthropicLLMClient(model=model)


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


async def _sse_stream(
    payload: ChatResponse,
    *,
    on_finalized: Callable[[], None] = lambda: None,
) -> AsyncIterator[bytes]:
    """The buffered SSE frame sequence: meta, then verified text, then verdict.

    ``on_finalized`` fires only once the last (verdict) frame has been handed
    off to the caller — i.e. after Starlette has already sent it and resumes
    this generator to ask for the next item (T013 criterion 4: "after the
    last frame is emitted", not merely constructed).
    """
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
    on_finalized()


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
    chat_model: str | None = None,
    chat_max_steps: int = DEFAULT_CHAT_MAX_STEPS,
    chat_llm_timeout: float = DEFAULT_LLM_TIMEOUT_SECONDS,
    conversation_store: ConversationStore | None = None,
    conversation_ttl_seconds: float = 2 * 60 * 60,
    clock: Callable[[], datetime] = _default_clock,
    audit_bridge: AuditBridgeClient | None = None,
    audit_sequence_recorder: Callable[[str], None] | None = None,
    tracer: Tracer | None = None,
    metrics: TelemetryMetrics | None = None,
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
    fake for both, so the suite makes no network call. ``chat_model`` omitted
    resolves via ``default_chat_model()`` (default ``claude-sonnet-5``,
    env-overridable via ``CHAT_MODEL`` — T043); the one resolved value is
    threaded to both the default LLM client construction and the
    ``AgentLoop`` model, so telemetry/cost lookups never disagree with the
    model actually called. ``conversation_store``
    is the Redis-shaped state backend; omitted, an in-process store is built
    from ``conversation_ttl_seconds`` (default 2h, ARCHITECTURE.md §7) and
    ``clock`` (an injected clock, for deterministic TTL tests). ``audit_bridge``
    is the T013 audit-bridge client; omitted, one is built from the
    ``AUDIT_BRIDGE_URL`` env var if set, else audit dispatch is disabled
    (ARCHITECTURE.md §7's fail-open extends to missing configuration — this
    never blocks ``/chat``). ``audit_sequence_recorder`` is a test-only
    ordering seam (default a no-op) asserting ``response_finalized`` precedes
    ``audit_post_attempted``. ``tracer`` is the T014 OTel tracer seam; omitted,
    it is resolved once via :mod:`copilot.telemetry.bootstrap` (env-configured,
    inert with no exporter configured). ``metrics`` is the T014 counters
    object backing ``/metrics``; omitted, a fresh :class:`TelemetryMetrics` is
    created and — unless the caller supplied its own ``audit_bridge`` — also
    wired into the default audit-bridge client, so both tool-failure and
    audit-delivery counts land on the same ``/metrics`` snapshot.
    """
    install_correlation_log_record_factory()

    checkers: Mapping[str, Checker] = (
        readiness_checkers if readiness_checkers is not None else default_checkers()
    )
    deadline = readiness_deadline if readiness_deadline is not None else default_deadline()

    resolved_chat_model: str = (
        chat_model if chat_model is not None else default_chat_model()
    )
    resolved_llm: LLMClient = (
        chat_llm if chat_llm is not None else _default_chat_llm(resolved_chat_model)
    )
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
    resolved_metrics: TelemetryMetrics = (
        metrics if metrics is not None else TelemetryMetrics()
    )
    resolved_audit_bridge: AuditBridgeClient | None = (
        audit_bridge
        if audit_bridge is not None
        else _default_audit_bridge(resolved_metrics)
    )
    resolved_audit_sequence_recorder: Callable[[str], None] = (
        audit_sequence_recorder
        if audit_sequence_recorder is not None
        else _noop_sequence_recorder
    )
    resolved_tracer: Tracer = tracer if tracer is not None else _default_tracer()

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

    @app.get("/metrics")
    def metrics_endpoint() -> JSONResponse:
        """T014 criterion 6: counters only — never request-scoped data.

        Not request-scoped: carries no patient id, conversation id,
        correlation id, or token — a metrics scrape must never become a PHI
        channel (ARCHITECTURE.md §7).
        """
        return JSONResponse(content=resolved_metrics.snapshot())

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
            model=resolved_chat_model,
            correlation_id=correlation_id,
            max_steps=chat_max_steps,
            llm_timeout=chat_llm_timeout,
            tracer=resolved_tracer,
            tool_metrics=resolved_metrics,
        )
        result = await agent_loop.run(chat_request.message, history=history)

        # T014 criterion 3: at least one PHI-free log record per request,
        # correlation-ID-stamped automatically by the T001 factory installed
        # above — never pass `correlation_id` via `extra` (it would collide).
        _logger.info(
            "chat_request_completed",
            extra={
                "outcome": (
                    "degraded"
                    if result.is_degraded
                    else "fallback" if result.is_fallback else "answered"
                )
            },
        )

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

        # T013: one audit-bridge invocation record per completed turn (this
        # point is only reached once validation and scope binding already
        # succeeded — a 422 or a 404 returns/raises long before here, so
        # neither ever produces a record). Built from `result` directly,
        # covering all three completed-turn shapes: degraded, T010 fallback,
        # and answered (including the verifier's own fallback-triggered path).
        background_tasks = BackgroundTasks()
        if resolved_audit_bridge is not None:
            audit_record = _build_audit_record(
                user_token_hash=token_hash,
                patient_id=record.patient_id,
                correlation_id=correlation_id,
                conversation_id=conversation_id,
                occurred_at=clock(),
                result=result,
            )
            background_tasks.add_task(
                _dispatch_audit,
                resolved_audit_bridge,
                audit_record,
                resolved_audit_sequence_recorder,
                chat_request.token,
            )

        if stream:

            def _on_sse_finalized() -> None:
                resolved_audit_sequence_recorder("response_finalized")

            return StreamingResponse(
                _sse_stream(payload, on_finalized=_on_sse_finalized),
                media_type="text/event-stream",
                background=background_tasks,
            )

        content = payload.model_dump(mode="json", exclude_none=True)
        resolved_audit_sequence_recorder("response_finalized")
        return JSONResponse(
            status_code=200,
            content=content,
            background=background_tasks,
        )

    return app


app = create_app()
