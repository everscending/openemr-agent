"""Tests for the ``/chat`` endpoint — scoped conversation state (T011).

Criteria map (see .tdd-swarm/tickets/T011-chat-endpoint.md):
  1.  Pydantic-validated request; malformed (missing/wrong-typed/extra field)
      -> 422, never 500, and the LLM is never invoked.
  2.  A new conversation returns a conversation_id; a follow-up with that id
      runs the loop with prior turns included (scripted fake LLM receives the
      earlier user/assistant turns on its later call).
  3.  Scope mismatch (different patient_id or different user identity) is
      rejected 404, indistinguishable from unknown/expired; the design
      decision resolves "pick one" to 404 (never 403 — an existence oracle).
  4.  The state store is behind a get/put/expire interface (Redis-shaped);
      the in-memory implementation enforces a configurable TTL against an
      injected clock; expiry deletes (a second read cannot resurrect).
  5.  Responses carry the correlation ID (body + header) and the
      machine-readable T008/T009 verification counts.
  6.  JSON mode and buffered SSE mode both exist; SSE emits meta, then the
      verified message, then a terminal verdict event; verification runs to
      completion before any claim-bearing byte is emitted.
  +   Never store or emit the raw bearer token (identity is sha256(token)).

Production code (the new conversation store module and the ``/chat`` route)
is imported lazily inside test bodies where it is genuinely new, so
collection succeeds before the implementation exists.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

from copilot.agent import ports
from copilot.agent.tools import ToolRegistry
from copilot.app import create_app
from copilot.correlation import CORRELATION_ID_HEADER

AWARE_T0 = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Lazy module accessor for the genuinely-new conversation store module
# ---------------------------------------------------------------------------


def store_mod() -> Any:
    from copilot.conversation import store

    return store


# ---------------------------------------------------------------------------
# Scripted fake LLM (mirrors tests/test_agent_loop.py's shape)
# ---------------------------------------------------------------------------


class ScriptedLLM:
    """Returns canned responses in order; records every call's arguments."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[SimpleNamespace] = []

    async def complete(self, *, system: str, messages: Any, tools: Any) -> Any:
        self.calls.append(
            SimpleNamespace(system=system, messages=list(messages), tools=list(tools))
        )
        if not self._responses:
            raise AssertionError("LLM called more times than scripted")
        return self._responses.pop(0)

    @property
    def call_count(self) -> int:
        return len(self.calls)


def final(text: str | None) -> Any:
    return ports.LLMResponse(stop_reason=ports.StopReason.END_TURN, text=text)


def empty_registry() -> ToolRegistry:
    return ToolRegistry([])


class FakeClock:
    """A settable clock — injected wherever the store needs ``now()``."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)


def make_client(
    llm: Any,
    *,
    store: Any | None = None,
    conversation_ttl_seconds: float = 2 * 60 * 60,
    clock: Any | None = None,
) -> TestClient:
    kwargs: dict[str, Any] = {
        "chat_llm": llm,
        "chat_registry_factory": lambda token: empty_registry(),
    }
    if store is not None:
        kwargs["conversation_store"] = store
    else:
        kwargs["conversation_ttl_seconds"] = conversation_ttl_seconds
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


# ==========================================================================
# Criterion 1 — malformed request -> 422, never 500, LLM never invoked
# ==========================================================================


def test_missing_required_field_returns_422_and_never_calls_the_llm() -> None:
    llm = ScriptedLLM([final("unused")])
    client = make_client(llm)
    resp = client.post(
        "/chat", json={"patient_id": "pat-1", "token": "tok"}  # message missing
    )
    assert resp.status_code == 422
    assert llm.call_count == 0


def test_wrong_typed_field_returns_422_and_never_calls_the_llm() -> None:
    llm = ScriptedLLM([final("unused")])
    client = make_client(llm)
    resp = client.post(
        "/chat",
        json={"message": "hi", "patient_id": 12345, "token": "tok"},
    )
    assert resp.status_code == 422
    assert llm.call_count == 0


def test_unknown_extra_field_returns_422_and_never_calls_the_llm() -> None:
    llm = ScriptedLLM([final("unused")])
    client = make_client(llm)
    resp = client.post(
        "/chat",
        json={
            "message": "hi",
            "patient_id": "pat-1",
            "token": "tok",
            "unexpected_field": "surprise",
        },
    )
    assert resp.status_code == 422
    assert llm.call_count == 0


# ==========================================================================
# Criterion 2 — new conversation id; follow-up replays prior turns
# ==========================================================================


def test_new_conversation_returns_id_and_follow_up_includes_prior_turns() -> None:
    llm = ScriptedLLM(
        [
            final("I reviewed the labs."),
            final("I checked her medications."),
        ]
    )
    client = make_client(llm)

    first = client.post("/chat", json=chat_body("Catch me up."))
    assert first.status_code == 200
    first_body = first.json()
    conv_id = first_body["conversation_id"]
    assert conv_id
    assert first_body["reply"] == "I reviewed the labs."

    second = client.post(
        "/chat",
        json=chat_body("What about her allergies?", conversation_id=conv_id),
    )
    assert second.status_code == 200
    assert second.json()["conversation_id"] == conv_id

    assert llm.call_count == 2
    second_turn_messages = llm.calls[1].messages
    contents = [m.content for m in second_turn_messages]
    assert any("Catch me up." in c for c in contents), contents
    assert any("I reviewed the labs." in c for c in contents), contents


# ==========================================================================
# Criterion 3 + mandatory adversarial — scope mismatch -> 404, indistinguishable
# ==========================================================================


def test_follow_up_with_different_patient_id_returns_404_and_original_still_usable() -> None:
    llm = ScriptedLLM([final("I reviewed the labs."), final("I checked allergies.")])
    client = make_client(llm)

    first = client.post("/chat", json=chat_body(patient_id="pat-1"))
    conv_id = first.json()["conversation_id"]

    mismatch = client.post(
        "/chat",
        json=chat_body("Show me something", patient_id="pat-2", conversation_id=conv_id),
    )
    assert mismatch.status_code == 404

    # Not destroyed: the rightful owner can still use it afterwards.
    follow_up = client.post(
        "/chat",
        json=chat_body("Any allergies?", patient_id="pat-1", conversation_id=conv_id),
    )
    assert follow_up.status_code == 200
    assert follow_up.json()["conversation_id"] == conv_id
    assert llm.call_count == 2  # the mismatched attempt never reached the LLM


def test_follow_up_with_different_token_returns_404() -> None:
    llm = ScriptedLLM([final("I reviewed the labs."), final("I checked allergies.")])
    client = make_client(llm)

    first = client.post("/chat", json=chat_body(token="token-A"))
    conv_id = first.json()["conversation_id"]

    mismatch = client.post(
        "/chat", json=chat_body("hi", token="token-B", conversation_id=conv_id)
    )
    assert mismatch.status_code == 404

    follow_up = client.post(
        "/chat", json=chat_body("allergies?", token="token-A", conversation_id=conv_id)
    )
    assert follow_up.status_code == 200
    assert llm.call_count == 2


def test_unknown_conversation_id_returns_404() -> None:
    llm = ScriptedLLM([])
    client = make_client(llm)
    resp = client.post("/chat", json=chat_body(conversation_id="does-not-exist"))
    assert resp.status_code == 404
    assert llm.call_count == 0


def test_the_four_404_bodies_are_byte_identical() -> None:
    """Unknown, expired, wrong-patient, and wrong-user all render one body."""
    llm = ScriptedLLM([final("first"), final("second")])
    clock = FakeClock(AWARE_T0)
    store = store_mod().InMemoryConversationStore(ttl_seconds=5, now=clock)
    client = make_client(llm, store=store)

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

    bodies = {unknown.text, wrong_patient.text, wrong_token.text, expired.text}
    assert len(bodies) == 1, bodies


# ==========================================================================
# Criterion 4 — get/put/expire interface; TTL against an injected clock
# ==========================================================================


def test_store_get_put_and_ttl_expiry_do_not_resurrect() -> None:
    smod = store_mod()
    clock = FakeClock(AWARE_T0)
    store = smod.InMemoryConversationStore(ttl_seconds=60, now=clock)
    record = smod.ConversationRecord(patient_id="pat-1", user_token_hash="hash-1")

    store.put("conv-1", record)
    assert store.get("conv-1") == record

    clock.advance(59)
    assert store.get("conv-1") == record  # not yet expired

    clock.advance(2)  # cumulative 61s since put -> past the 60s ttl
    assert store.get("conv-1") is None
    assert store.get("conv-1") is None  # still gone: deleted, not resurrected
    assert len(store) == 0


def test_store_expire_deletes_immediately_regardless_of_remaining_ttl() -> None:
    smod = store_mod()
    clock = FakeClock(AWARE_T0)
    store = smod.InMemoryConversationStore(ttl_seconds=3600, now=clock)
    record = smod.ConversationRecord(patient_id="pat-1", user_token_hash="hash-1")
    store.put("conv-1", record)

    store.expire("conv-1")

    assert store.get("conv-1") is None


def test_endpoint_conversation_read_one_second_past_ttl_returns_404_and_stays_gone() -> None:
    llm = ScriptedLLM([final("first turn reply")])
    clock = FakeClock(AWARE_T0)
    store = store_mod().InMemoryConversationStore(ttl_seconds=60, now=clock)
    client = make_client(llm, store=store)

    first = client.post("/chat", json=chat_body())
    conv_id = first.json()["conversation_id"]

    clock.advance(61)  # one second past the 60s ttl

    expired_1 = client.post("/chat", json=chat_body(conversation_id=conv_id))
    assert expired_1.status_code == 404
    expired_2 = client.post("/chat", json=chat_body(conversation_id=conv_id))
    assert expired_2.status_code == 404
    assert llm.call_count == 1  # neither expired attempt reached the LLM


# ==========================================================================
# Criterion 5 — correlation ID + machine-readable verification counts
# ==========================================================================


def test_response_carries_correlation_id_in_body_and_header() -> None:
    llm = ScriptedLLM([final("I reviewed the labs.")])
    client = make_client(llm)
    resp = client.post(
        "/chat", json=chat_body(), headers={CORRELATION_ID_HEADER: "corr-fixed-123"}
    )
    assert resp.status_code == 200
    assert resp.headers[CORRELATION_ID_HEADER] == "corr-fixed-123"
    assert resp.json()["correlation_id"] == "corr-fixed-123"


def test_response_body_has_the_expected_shape_with_verification_counts() -> None:
    llm = ScriptedLLM([final("I reviewed the labs.")])
    client = make_client(llm)
    resp = client.post("/chat", json=chat_body())
    body = resp.json()
    assert set(body.keys()) == {
        "conversation_id",
        "correlation_id",
        "reply",
        "verification",
        "fallback",
    }
    counts = body["verification"]
    assert counts["claims_total"] == counts["claims_passed"] + counts["claims_stripped"]
    assert set(counts.keys()) >= {
        "claims_total",
        "claims_passed",
        "claims_stripped",
        "numeric_checked",
        "numeric_unchecked",
    }


# ==========================================================================
# Criterion 6 — JSON mode + buffered SSE mode; verified content only
# ==========================================================================


def test_json_mode_returns_a_single_response_with_the_full_reply() -> None:
    llm = ScriptedLLM([final("I reviewed the labs.")])
    client = make_client(llm)
    resp = client.post("/chat", json=chat_body())
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json()["reply"] == "I reviewed the labs."


def test_sse_mode_emits_meta_then_message_then_verdict_in_order() -> None:
    llm = ScriptedLLM([final("I reviewed the labs.")])
    client = make_client(llm)
    resp = client.post("/chat", params={"stream": "true"}, json=chat_body())
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    text = resp.text
    meta_idx = text.index("event: meta")
    message_idx = text.index("event: message")
    verdict_idx = text.index("event: verdict")
    assert meta_idx < message_idx < verdict_idx
    assert "I reviewed the labs." in text

    meta_frame = text[:message_idx]
    assert "conversation_id" in meta_frame
    assert "correlation_id" in meta_frame


def test_fully_stripped_draft_emits_fallback_and_never_the_stripped_text() -> None:
    """Mandatory adversarial test: buffered verification, not a passthrough."""
    from copilot.verification import FALLBACK_TEXT as VERIFICATION_FALLBACK_TEXT

    llm_json = ScriptedLLM([final("She is allergic to penicillin.")])  # uncited claim
    client_json = make_client(llm_json)
    json_resp = client_json.post("/chat", json=chat_body())
    assert json_resp.status_code == 200
    assert "penicillin" not in json_resp.text
    assert VERIFICATION_FALLBACK_TEXT in json_resp.json()["reply"]

    llm_sse = ScriptedLLM([final("She is allergic to penicillin.")])
    client_sse = make_client(llm_sse)
    sse_resp = client_sse.post("/chat", params={"stream": "true"}, json=chat_body())
    assert sse_resp.status_code == 200
    assert "penicillin" not in sse_resp.text
    assert VERIFICATION_FALLBACK_TEXT in sse_resp.text


# ==========================================================================
# Mandatory adversarial — the bearer token leaks nowhere
# ==========================================================================


def test_bearer_token_never_appears_in_response_body_sse_or_stored_record() -> None:
    smod = store_mod()
    secret_token = "SUPER-SECRET-BEARER-TOKEN-xyz789"
    llm = ScriptedLLM(
        [final("I reviewed the labs."), final("I checked her medications.")]
    )
    store = smod.InMemoryConversationStore(ttl_seconds=3600)
    client = make_client(llm, store=store)

    first = client.post("/chat", json=chat_body(token=secret_token))
    assert first.status_code == 200
    assert secret_token not in first.text
    conv_id = first.json()["conversation_id"]

    sse = client.post(
        "/chat",
        params={"stream": "true"},
        json=chat_body("follow-up", token=secret_token, conversation_id=conv_id),
    )
    assert secret_token not in sse.text

    record = store.get(conv_id)
    assert record is not None
    assert secret_token not in record.model_dump_json()
    assert record.user_token_hash == hashlib.sha256(secret_token.encode()).hexdigest()
    assert record.user_token_hash != secret_token


# ==========================================================================
# Adversarial probes of my own devising (beyond the mandated list)
# ==========================================================================


def test_ttl_boundary_equality_is_treated_as_expired_not_one_tick_early() -> None:
    """Probe the exact >= boundary: valid a hair under ttl, gone exactly at it."""
    smod = store_mod()
    clock = FakeClock(AWARE_T0)
    store = smod.InMemoryConversationStore(ttl_seconds=60, now=clock)
    record = smod.ConversationRecord(patient_id="pat-1", user_token_hash="hash-1")
    store.put("conv-1", record)

    clock.advance(59.999)
    assert store.get("conv-1") is not None  # still just inside the window

    clock.advance(0.001)  # now exactly at the 60s boundary
    assert store.get("conv-1") is None


def test_two_independent_conversations_do_not_leak_turns_into_each_other() -> None:
    """Probe cross-conversation isolation absent any mismatch/attack — plain
    concurrent use of the same store must not bleed history between scopes."""
    llm = ScriptedLLM(
        [
            final("conv A turn 1 reply"),
            final("conv B turn 1 reply"),
            final("conv A turn 2 reply"),
        ]
    )
    client = make_client(llm)

    a1 = client.post(
        "/chat", json=chat_body("A question one", patient_id="pat-A", token="token-A")
    )
    conv_a = a1.json()["conversation_id"]

    b1 = client.post(
        "/chat", json=chat_body("B question one", patient_id="pat-B", token="token-B")
    )
    conv_b = b1.json()["conversation_id"]
    assert conv_a != conv_b

    client.post(
        "/chat",
        json=chat_body(
            "A question two", patient_id="pat-A", token="token-A", conversation_id=conv_a
        ),
    )

    third_call_messages = llm.calls[2].messages
    contents = [m.content for m in third_call_messages]
    assert any("A question one" in c for c in contents), contents
    assert not any("B question one" in c for c in contents), contents
    assert not any("conv B turn 1 reply" in c for c in contents), contents


def test_new_conversation_with_same_patient_and_token_does_not_inherit_old_history() -> None:
    """Probe that scope alone never substitutes for an explicit conversation_id:
    omitting conversation_id must start fresh even when (patient, user) match
    an existing conversation exactly."""
    llm = ScriptedLLM(
        [
            final("first conversation reply"),
            final("second, unrelated conversation reply"),
        ]
    )
    client = make_client(llm)

    first = client.post(
        "/chat", json=chat_body("first question", patient_id="pat-1", token="token-X")
    )
    assert first.json()["conversation_id"]

    second = client.post(
        "/chat", json=chat_body("brand new question", patient_id="pat-1", token="token-X")
    )
    assert second.status_code == 200
    assert second.json()["conversation_id"] != first.json()["conversation_id"]

    second_call_messages = llm.calls[1].messages
    contents = [m.content for m in second_call_messages]
    assert not any("first question" in c for c in contents), contents
    assert not any("first conversation reply" in c for c in contents), contents
