"""Tests for the audit-bridge client — fail-open-with-alarm (T013).

Criteria map (see .tdd-swarm/tickets/T013-audit-bridge-client.md):
  1.  After each completed ``/chat`` turn (success, fallback, or
      verification-fallback alike), an invocation record is POSTed to a
      configured bridge URL: user identity (T011 sha256(token) hash),
      patient_id, correlation ID, conversation ID, verification verdict
      counts, degraded/fallback markers, timestamp. Including the T012
      degraded path.
  2.  The record carries no PHI: the model's field set is closed (no
      free-text beyond bounded identifiers/enums/ints), and a PHI-laden
      conversation's serialized record contains none of the sentinel
      strings.
  3.  Bridge 5xx / timeout / connection-refused -> the user response is
      unchanged and exactly one ``audit_bridge_delivery_failed`` alert is
      emitted carrying the correlation ID. All three failure shapes, JSON
      and SSE.
  4.  The audit POST is dispatched after the response is finalized
      (background task) -- asserted with an injected sequence recorder,
      never with timing.
  5.  Delivery success/failure is counted via an injected metrics recorder:
      exactly one of success/failure per attempt.

Design decisions pinned here (orchestrator-authored, not relitigated):
  * Fail-open is asserted by byte-identity of the response body, not status
    code.
  * The record's ``user`` field is the T011 sha256(token) hash; the raw
    token appears nowhere.
  * PHI exclusion is enforced by the closed field set (``extra="forbid"``).
  * The alert carries only the correlation ID and the HTTP status OR the
    exception *type name* -- never the record body, an exception message, or
    response text.
  * 422 and 404 (all scope-mismatch shapes) produce zero bridge calls.
  * Exactly one metrics counter per attempt.

Production code (the new ``copilot.audit`` package and the ``/chat`` route's
audit-dispatch wiring) is referenced lazily inside test bodies so collection
succeeds before the implementation exists (RED = the missing feature per
test).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from copilot.agent import ports
from copilot.agent.tools import ToolRegistry
from copilot.app import create_app
from copilot.conversation import hash_token
from copilot.correlation import CORRELATION_ID_HEADER

BRIDGE_URL = "https://openemr.example.test/apis/default/copilot/audit-bridge"


# ---------------------------------------------------------------------------
# Lazy module accessor for the genuinely-new audit module
# ---------------------------------------------------------------------------


def audit_mod() -> Any:
    import copilot.audit as audit

    return audit


def store_mod() -> Any:
    from copilot.conversation import store

    return store


# ---------------------------------------------------------------------------
# Scripted fake LLM + response builders (mirrors test_chat.py's shapes)
# ---------------------------------------------------------------------------


class ScriptedLLM:
    """Returns canned responses in order; records every call's arguments."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[SimpleNamespace] = []

    async def complete(self, *, system: str, messages: Any, tools: Any) -> Any:
        self.calls.append(SimpleNamespace(messages=list(messages)))
        if not self._responses:
            raise AssertionError("LLM called more times than scripted")
        return self._responses.pop(0)

    @property
    def call_count(self) -> int:
        return len(self.calls)


class UnavailableLLM:
    """Raises the T012 typed unavailability error on every call (degraded path)."""

    async def complete(self, *, system: str, messages: Any, tools: Any) -> Any:
        raise ports.LLMUnavailable("provider down")


def final(text: str | None) -> Any:
    return ports.LLMResponse(stop_reason=ports.StopReason.END_TURN, text=text)


def refusal() -> Any:
    return ports.LLMResponse(stop_reason=ports.StopReason.REFUSAL)


def empty_registry() -> ToolRegistry:
    return ToolRegistry([])


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)


AWARE_T0 = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Metrics + sequence recorder fakes
# ---------------------------------------------------------------------------


class FakeMetricsRecorder:
    def __init__(self) -> None:
        self.successes = 0
        self.failures = 0

    def record_success(self) -> None:
        self.successes += 1

    def record_failure(self) -> None:
        self.failures += 1

    @property
    def total_attempts(self) -> int:
        return self.successes + self.failures


class SequenceRecorder:
    def __init__(self) -> None:
        self.events: list[str] = []

    def __call__(self, event: str) -> None:
        self.events.append(event)


# ---------------------------------------------------------------------------
# Mock transports for the bridge endpoint
# ---------------------------------------------------------------------------


def healthy_transport(captured: list[bytes]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.content)
        return httpx.Response(200, json={"status": "recorded"})

    return httpx.MockTransport(handler)


def failing_500_transport(captured: list[bytes] | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.append(request.content)
        return httpx.Response(500, json={"error": "internal"})

    return httpx.MockTransport(handler)


def hanging_transport(hang_seconds: float) -> httpx.MockTransport:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(hang_seconds)
        return httpx.Response(200, json={"status": "too late"})

    return httpx.MockTransport(handler)


def connection_refused_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# App / bridge-client builders
# ---------------------------------------------------------------------------


def bridge_client(
    transport: httpx.AsyncBaseTransport,
    *,
    metrics: Any | None = None,
    timeout: float = 2.0,
    logger: logging.Logger | None = None,
) -> Any:
    audit = audit_mod()
    return audit.AuditBridgeClient(
        base_url=BRIDGE_URL,
        transport=transport,
        metrics=metrics if metrics is not None else FakeMetricsRecorder(),
        timeout=timeout,
        logger=logger,
    )


def make_client(
    llm: Any,
    *,
    store: Any | None = None,
    audit_bridge: Any | None = None,
    audit_sequence_recorder: Any | None = None,
    clock: Any | None = None,
) -> TestClient:
    kwargs: dict[str, Any] = {
        "chat_llm": llm,
        "chat_registry_factory": lambda token: empty_registry(),
    }
    if store is not None:
        kwargs["conversation_store"] = store
    if audit_bridge is not None:
        kwargs["audit_bridge"] = audit_bridge
    if audit_sequence_recorder is not None:
        kwargs["audit_sequence_recorder"] = audit_sequence_recorder
    if clock is not None:
        kwargs["clock"] = clock
    return TestClient(create_app(**kwargs))


def chat_body(
    message: str = "Catch me up.",
    *,
    patient_id: str = "pat-1",
    token: str = "user-token-abc",
    conversation_id: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "message": message,
        "patient_id": patient_id,
        "token": token,
    }
    if conversation_id is not None:
        body["conversation_id"] = conversation_id
    return body


def seeded_store(
    conversation_id: str, *, patient_id: str = "pat-1", token: str = "user-token-abc"
) -> Any:
    """A store pre-seeded with one bound, zero-turn conversation at a fixed id.

    Used so a "follow-up" call resolves to a deterministic, caller-chosen
    conversation_id -- letting byte-identity comparisons hold across
    independently-configured apps (each would otherwise mint its own random
    uuid4 on a fresh conversation).
    """
    smod = store_mod()
    store = smod.InMemoryConversationStore(ttl_seconds=3600)
    store.put(
        conversation_id,
        smod.ConversationRecord(
            patient_id=patient_id, user_token_hash=hash_token(token)
        ),
    )
    return store


FIXED_CONV_ID = "conv-fixed-0001"
FIXED_CORR_ID = "corr-fixed-0001"


def post_with_bridge(
    audit_bridge: Any,
    *,
    message: str = "Catch me up.",
    llm_text: str = "I reviewed the labs.",
    stream: bool = False,
) -> Any:
    llm = ScriptedLLM([final(llm_text)])
    store = seeded_store(FIXED_CONV_ID)
    client = make_client(llm, store=store, audit_bridge=audit_bridge)
    params = {"stream": "true"} if stream else {}
    return client.post(
        "/chat",
        params=params,
        headers={CORRELATION_ID_HEADER: FIXED_CORR_ID},
        json=chat_body(message, conversation_id=FIXED_CONV_ID),
    )


# ==========================================================================
# Criterion 1 -- invocation record posted after each completed turn
# ==========================================================================


def test_answered_turn_posts_audit_record_with_expected_fields() -> None:
    captured: list[bytes] = []
    bridge = bridge_client(healthy_transport(captured))
    resp = post_with_bridge(bridge, llm_text="I reviewed the labs.")
    assert resp.status_code == 200
    assert len(captured) == 1

    record = json.loads(captured[0])
    assert record["user_token_hash"] == hash_token("user-token-abc")
    assert record["patient_id"] == "pat-1"
    assert record["correlation_id"] == FIXED_CORR_ID
    assert record["conversation_id"] == FIXED_CONV_ID
    assert record["outcome"] == "answered"
    assert record["degraded"] is None
    assert record["fallback_reason"] is None
    assert record["claims_total"] == record["claims_passed"] + record["claims_stripped"]
    assert "occurred_at" in record and record["occurred_at"]


def test_degraded_first_turn_posts_audit_record_with_degraded_marker() -> None:
    captured: list[bytes] = []
    bridge = bridge_client(healthy_transport(captured))
    llm = UnavailableLLM()
    client = make_client(llm, audit_bridge=bridge)
    resp = client.post(
        "/chat",
        headers={CORRELATION_ID_HEADER: FIXED_CORR_ID},
        json=chat_body(),
    )
    assert resp.status_code == 200
    assert resp.json()["degraded"] == "llm_unavailable"
    assert len(captured) == 1

    record = json.loads(captured[0])
    assert record["outcome"] == "degraded"
    assert record["degraded"] == "llm_unavailable"
    assert record["fallback_reason"] is None
    assert record["claims_total"] == 0
    assert record["claims_passed"] == 0
    assert record["claims_stripped"] == 0


# ==========================================================================
# Criterion 2 -- no PHI content; enforced by schema and by serialization
# ==========================================================================


def test_record_field_set_is_closed_around_ids_enums_ints_and_one_timestamp() -> None:
    audit = audit_mod()
    fields = set(audit.AuditInvocationRecord.model_fields)
    assert fields == {
        "user_token_hash",
        "patient_id",
        "correlation_id",
        "conversation_id",
        "occurred_at",
        "claims_total",
        "claims_passed",
        "claims_stripped",
        "outcome",
        "degraded",
        "fallback_reason",
    }
    # extra="forbid" is inherited from ContractModel -- confirm it wasn't
    # loosened for this model specifically.
    assert audit.AuditInvocationRecord.model_config.get("extra") == "forbid"


def test_phi_laden_conversation_leaves_no_sentinel_in_serialized_record() -> None:
    sentinel_question = "does she still take penicillin"
    sentinel_medication = "amoxicillin-clavulanate-XYZ"
    sentinel_reply = f"Yes, she takes {sentinel_medication} 500mg twice daily."
    secret_token = "SUPER-SECRET-BEARER-TOKEN-xyz789"

    captured: list[bytes] = []
    bridge = bridge_client(healthy_transport(captured))
    llm = ScriptedLLM([final(sentinel_reply)])
    client = make_client(llm, audit_bridge=bridge)
    resp = client.post(
        "/chat",
        json=chat_body(sentinel_question, token=secret_token),
    )
    assert resp.status_code == 200
    assert len(captured) == 1

    raw = captured[0].decode("utf-8")
    assert sentinel_question not in raw
    assert sentinel_medication not in raw
    assert "penicillin" not in raw
    assert secret_token not in raw
    assert hash_token(secret_token) in raw  # the hash IS expected to be present


# ==========================================================================
# Criterion 3 + mandatory adversarial -- fail-open, byte-identical, one alert
# ==========================================================================


def test_bridge_500_leaves_json_response_byte_identical_and_alerts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    healthy_resp = post_with_bridge(bridge_client(healthy_transport([])))

    metrics = FakeMetricsRecorder()
    logger = logging.getLogger("copilot.tests.audit.500")
    with caplog.at_level(logging.ERROR, logger=logger.name):
        failing_resp = post_with_bridge(
            bridge_client(failing_500_transport(), metrics=metrics, logger=logger)
        )

    assert failing_resp.status_code == healthy_resp.status_code == 200
    assert failing_resp.content == healthy_resp.content

    alerts = [r for r in caplog.records if r.name == logger.name]
    assert len(alerts) == 1
    assert alerts[0].getMessage() == "audit_bridge_delivery_failed"
    assert alerts[0].correlation_id == FIXED_CORR_ID
    assert metrics.failures == 1
    assert metrics.successes == 0


def test_bridge_timeout_leaves_json_response_byte_identical_and_alerts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    healthy_resp = post_with_bridge(bridge_client(healthy_transport([])))

    metrics = FakeMetricsRecorder()
    logger = logging.getLogger("copilot.tests.audit.timeout")
    with caplog.at_level(logging.ERROR, logger=logger.name):
        failing_resp = post_with_bridge(
            bridge_client(
                hanging_transport(5.0), metrics=metrics, timeout=0.05, logger=logger
            )
        )

    assert failing_resp.status_code == healthy_resp.status_code == 200
    assert failing_resp.content == healthy_resp.content

    alerts = [r for r in caplog.records if r.name == logger.name]
    assert len(alerts) == 1
    assert alerts[0].correlation_id == FIXED_CORR_ID
    assert metrics.failures == 1
    assert metrics.successes == 0


def test_bridge_connection_refused_leaves_json_response_byte_identical_and_alerts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    healthy_resp = post_with_bridge(bridge_client(healthy_transport([])))

    metrics = FakeMetricsRecorder()
    logger = logging.getLogger("copilot.tests.audit.refused")
    with caplog.at_level(logging.ERROR, logger=logger.name):
        failing_resp = post_with_bridge(
            bridge_client(
                connection_refused_transport(), metrics=metrics, logger=logger
            )
        )

    assert failing_resp.status_code == healthy_resp.status_code == 200
    assert failing_resp.content == healthy_resp.content

    alerts = [r for r in caplog.records if r.name == logger.name]
    assert len(alerts) == 1
    assert alerts[0].correlation_id == FIXED_CORR_ID
    assert metrics.failures == 1
    assert metrics.successes == 0


def test_sse_bridge_failures_leave_every_frame_unchanged() -> None:
    """Mandatory adversarial: the SSE path, all three failure shapes."""
    healthy_resp = post_with_bridge(bridge_client(healthy_transport([])), stream=True)
    assert healthy_resp.status_code == 200
    healthy_text = healthy_resp.text
    assert "event: meta" in healthy_text
    assert "event: message" in healthy_text
    assert "event: verdict" in healthy_text

    for transport in (
        failing_500_transport(),
        hanging_transport(5.0),
        connection_refused_transport(),
    ):
        resp = post_with_bridge(
            bridge_client(transport, timeout=0.05), stream=True
        )
        assert resp.status_code == 200
        assert resp.text == healthy_text, transport


# ==========================================================================
# Criterion 4 -- dispatched after the response is finalized
# ==========================================================================


def test_response_finalized_precedes_audit_post_attempted_json() -> None:
    seq = SequenceRecorder()
    captured: list[bytes] = []
    bridge = bridge_client(healthy_transport(captured))
    llm = ScriptedLLM([final("I reviewed the labs.")])
    client = make_client(llm, audit_bridge=bridge, audit_sequence_recorder=seq)

    resp = client.post("/chat", json=chat_body())
    assert resp.status_code == 200
    assert seq.events == ["response_finalized", "audit_post_attempted"]


def test_response_finalized_precedes_audit_post_attempted_sse() -> None:
    seq = SequenceRecorder()
    captured: list[bytes] = []
    bridge = bridge_client(healthy_transport(captured))
    llm = ScriptedLLM([final("I reviewed the labs.")])
    client = make_client(llm, audit_bridge=bridge, audit_sequence_recorder=seq)

    resp = client.post("/chat", params={"stream": "true"}, json=chat_body())
    assert resp.status_code == 200
    assert "event: verdict" in resp.text
    assert seq.events == ["response_finalized", "audit_post_attempted"]


# ==========================================================================
# Criterion (design decision) -- 422 / 404 produce zero bridge calls
# ==========================================================================


def test_malformed_request_422_produces_zero_bridge_calls() -> None:
    captured: list[bytes] = []
    bridge = bridge_client(healthy_transport(captured))
    llm = ScriptedLLM([final("unused")])
    client = make_client(llm, audit_bridge=bridge)

    resp = client.post(
        "/chat", json={"patient_id": "pat-1", "token": "tok"}  # message missing
    )
    assert resp.status_code == 422
    assert llm.call_count == 0
    assert captured == []


def test_scope_mismatch_404_produces_zero_bridge_calls_for_every_shape() -> None:
    captured: list[bytes] = []
    bridge = bridge_client(healthy_transport(captured))
    clock = FakeClock(AWARE_T0)
    smod = store_mod()
    store = smod.InMemoryConversationStore(ttl_seconds=5, now=clock)
    llm = ScriptedLLM([final("first"), final("second")])
    client = make_client(llm, store=store, audit_bridge=bridge, clock=clock)

    resp_a = client.post("/chat", json=chat_body(patient_id="pat-1", token="token-A"))
    conv_a = resp_a.json()["conversation_id"]
    resp_b = client.post("/chat", json=chat_body(patient_id="pat-2", token="token-B"))
    conv_b = resp_b.json()["conversation_id"]
    clock.advance(6)  # past conv_b's 5s ttl

    unknown = client.post("/chat", json=chat_body(conversation_id="totally-unknown"))
    wrong_patient = client.post(
        "/chat",
        json=chat_body(patient_id="pat-999", token="token-A", conversation_id=conv_a),
    )
    wrong_token = client.post(
        "/chat",
        json=chat_body(patient_id="pat-1", token="wrong-token", conversation_id=conv_a),
    )
    expired = client.post(
        "/chat",
        json=chat_body(patient_id="pat-2", token="token-B", conversation_id=conv_b),
    )

    for resp in (unknown, wrong_patient, wrong_token, expired):
        assert resp.status_code == 404

    # Exactly the two legitimate answered turns above reached the bridge;
    # none of the four rejected attempts did.
    assert len(captured) == 2


# ==========================================================================
# Mandatory adversarial -- degraded + refusal each -> exactly one record
# ==========================================================================


def test_degraded_turn_and_refusal_turn_each_produce_one_correctly_marked_record() -> None:
    audit = audit_mod()

    degraded_captured: list[bytes] = []
    degraded_bridge = bridge_client(healthy_transport(degraded_captured))
    degraded_client = make_client(UnavailableLLM(), audit_bridge=degraded_bridge)
    degraded_resp = degraded_client.post("/chat", json=chat_body())
    assert degraded_resp.status_code == 200
    assert len(degraded_captured) == 1
    degraded_record = json.loads(degraded_captured[0])
    assert degraded_record["outcome"] == "degraded"
    assert degraded_record["degraded"] == "llm_unavailable"
    assert degraded_record["fallback_reason"] is None
    assert degraded_record["claims_total"] == 0
    assert degraded_record["claims_passed"] == 0
    assert degraded_record["claims_stripped"] == 0

    refusal_captured: list[bytes] = []
    refusal_bridge = bridge_client(healthy_transport(refusal_captured))
    refusal_llm = ScriptedLLM([refusal()])
    refusal_client = make_client(refusal_llm, audit_bridge=refusal_bridge)
    refusal_resp = refusal_client.post("/chat", json=chat_body())
    assert refusal_resp.status_code == 200
    assert refusal_resp.json()["fallback"] is True
    assert len(refusal_captured) == 1
    refusal_record = json.loads(refusal_captured[0])
    assert refusal_record["outcome"] == "fallback"
    assert refusal_record["fallback_reason"] == "refusal"
    assert refusal_record["degraded"] is None
    assert refusal_record["claims_total"] == 0
    assert refusal_record["claims_passed"] == 0
    assert refusal_record["claims_stripped"] == 0

    # Both reasons are members of the pinned FallbackReason/DegradedReason
    # enums -- not ad hoc strings.
    assert audit.AuditOutcome("degraded") is audit.AuditOutcome.DEGRADED
    assert audit.AuditOutcome("fallback") is audit.AuditOutcome.FALLBACK


# ==========================================================================
# Mandatory adversarial -- alert content: status vs. exception type name
# ==========================================================================


def test_alert_for_500_carries_status_code_and_no_exception_type(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("copilot.tests.audit.alert.500")
    bridge = bridge_client(failing_500_transport(), logger=logger)
    with caplog.at_level(logging.ERROR, logger=logger.name):
        resp = post_with_bridge(bridge)
    assert resp.status_code == 200

    alerts = [r for r in caplog.records if r.name == logger.name]
    assert len(alerts) == 1
    assert alerts[0].status_code == 500
    assert alerts[0].exception_type is None
    # The alert body/message itself never carries the record or a message.
    assert "internal" not in alerts[0].getMessage()


def test_alert_for_timeout_carries_exception_type_name_and_not_its_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("copilot.tests.audit.alert.timeout")
    bridge = bridge_client(hanging_transport(5.0), timeout=0.05, logger=logger)
    with caplog.at_level(logging.ERROR, logger=logger.name):
        resp = post_with_bridge(bridge)
    assert resp.status_code == 200

    alerts = [r for r in caplog.records if r.name == logger.name]
    assert len(alerts) == 1
    assert alerts[0].status_code is None
    assert alerts[0].exception_type  # some type name is present
    assert isinstance(alerts[0].exception_type, str)
    # The log *message* itself is the bare, static event string -- never the
    # exception's str(), which for a TimeoutError could read like "" or
    # carry internal detail depending on how/where it was raised.
    assert alerts[0].getMessage() == "audit_bridge_delivery_failed"
    forbidden_message_fragments = ("timed out", "deadline", "seconds", "elapsed")
    rendered = alerts[0].getMessage()
    for fragment in forbidden_message_fragments:
        assert fragment not in rendered.lower()


# ==========================================================================
# Criterion 5 -- exactly one metrics counter per attempt
# ==========================================================================


def test_metrics_counter_increments_exactly_once_on_success() -> None:
    metrics = FakeMetricsRecorder()
    bridge = bridge_client(healthy_transport([]), metrics=metrics)
    resp = post_with_bridge(bridge)
    assert resp.status_code == 200
    assert metrics.successes == 1
    assert metrics.failures == 0


def test_metrics_counter_increments_exactly_once_on_failure() -> None:
    metrics = FakeMetricsRecorder()
    bridge = bridge_client(failing_500_transport(), metrics=metrics)
    resp = post_with_bridge(bridge)
    assert resp.status_code == 200
    assert metrics.successes == 0
    assert metrics.failures == 1


# ==========================================================================
# Adversarial probes of my own devising (beyond the mandated list)
# ==========================================================================


def test_verification_triggered_fallback_is_answered_outcome_not_fallback_outcome() -> None:
    """A response the *verifier* reduced to its fallback text (all claims
    stripped, T008/T009's own fallback_triggered) is a different axis from
    the T010 model-level FallbackRequired: the loop still returned a verdict,
    it never entered the FallbackRequired/degraded branches. The audit
    outcome must reflect that -- 'answered', with fallback_reason=None and
    degraded=None -- even though the *user-visible text* is the same generic
    fallback string T010 renders on its own axis. Conflating the two would
    make 'fallback_reason' ambiguous between two structurally different
    failure classes.
    """
    from copilot.verification import FALLBACK_TEXT as VERIFICATION_FALLBACK_TEXT

    captured: list[bytes] = []
    bridge = bridge_client(healthy_transport(captured))
    # Uncited clinical claim, no tools/refs available -> stripped entirely.
    resp = post_with_bridge(
        bridge, llm_text="She is allergic to penicillin.", message="Any allergies?"
    )
    assert resp.status_code == 200
    assert VERIFICATION_FALLBACK_TEXT in resp.json()["reply"]
    assert resp.json()["fallback"] is False  # not the T010 axis

    assert len(captured) == 1
    record = json.loads(captured[0])
    assert record["outcome"] == "answered"
    assert record["fallback_reason"] is None
    assert record["degraded"] is None
    assert record["claims_total"] == 1
    assert record["claims_passed"] == 0
    assert record["claims_stripped"] == 1


def test_metrics_recorder_never_double_counts_across_a_mixed_sequence() -> None:
    """Probe the 'never both, never neither' invariant across many attempts
    of mixed outcomes, not just one call each -- a per-call implementation
    bug (e.g. incrementing failure in a `finally` block after already
    incrementing success) would only show up with more than one call."""
    metrics = FakeMetricsRecorder()

    outcomes = [
        healthy_transport([]),
        failing_500_transport(),
        healthy_transport([]),
        connection_refused_transport(),
        hanging_transport(5.0),
        healthy_transport([]),
    ]
    for i, transport in enumerate(outcomes):
        timeout = 0.05 if i == 4 else 2.0
        bridge = bridge_client(transport, metrics=metrics, timeout=timeout)
        resp = post_with_bridge(bridge)
        assert resp.status_code == 200

    assert metrics.total_attempts == len(outcomes)
    assert metrics.successes == 3
    assert metrics.failures == 3


def test_hung_bridge_does_not_block_the_response_past_its_configured_timeout() -> None:
    """Probe wall-clock boundedness directly (not just eventual correctness):
    a bridge that hangs far longer than the configured timeout must not make
    the /chat call itself take anywhere near that long, even though
    TestClient awaits the background task before returning (Starlette runs
    BackgroundTasks inside the same ASGI call)."""
    import time

    bridge = bridge_client(hanging_transport(5.0), timeout=0.05)
    t0 = time.monotonic()
    resp = post_with_bridge(bridge)
    elapsed = time.monotonic() - t0

    assert resp.status_code == 200
    assert elapsed < 1.0, elapsed
