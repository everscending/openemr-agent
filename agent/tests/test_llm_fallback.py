"""Tests for the non-AI fallback — structured snapshot when the LLM is down (T012).

Criteria map (see .tdd-swarm/tickets/T012-llm-fallback.md):
  1.  LLM raises typed timeout/unavailability on the *first* turn -> /chat 200
      with a typed fallback payload: the raw T005 snapshot (fetched with no LLM
      involvement) + a machine-readable ``degraded: llm_unavailable`` marker.
  2.  The per-LLM-call timeout is configurable and enforced by the loop: a hung
      call is abandoned at the deadline (asserted with an injected sleep and a
      wall-clock bound well under the hang).
  3.  Failure *after* tools ran reuses the already-fetched snapshot — no refetch
      (asserted by transport call counts at the httpx.MockTransport seam).
  4.  Follow-up turns during an outage return a typed ``degraded`` response
      (no snapshot render), prior turns preserved byte-identical, and the same
      conversation answers normally once the LLM recovers.
  5.  The fallback payload passes verification trivially (only tool data, no
      model claims): counts all zero, coverage intact, never fed to the verifier.

Design decisions pinned here:
  * A pure error taxonomy on the port: ``LLMUnavailable`` (base), ``LLMTimeout``,
    ``LLMTransportError`` — no vendor exception types leak across the port.
  * ``degraded`` and ``FallbackReason`` are different axes; a response carries at
    most one.
  * Structured payloads are never run through ``verify_response``.
  * The failed turn is not appended; prior turns stay byte-identical.
  * Never a stack trace / exception class name / module path in any body or SSE.

Production code (the taxonomy, the loop timeout/degraded path, the endpoint
wiring) is referenced lazily inside test bodies so collection succeeds before
the implementation exists (RED = the missing feature per test).
"""

from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import httpx
from fastapi.testclient import TestClient

from copilot import contracts
from copilot.agent import ports
from copilot.agent.tools import Tool, ToolRegistry
from copilot.app import create_app
from copilot.contracts.tools import GetPatientSnapshotInput
from copilot.fhir import FhirClient

AWARE = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)
BASE_URL = "https://emr.example.test/apis/default/fhir"
TOKEN = "user-token"

EXPECTED_CATEGORIES = frozenset(
    {
        "demographics",
        "medications",
        "problems",
        "allergies",
        "labs",
        "last_encounter",
    }
)


# ---------------------------------------------------------------------------
# Lazy module accessors (called inside bodies, never at collection time)
# ---------------------------------------------------------------------------


def loop_mod() -> Any:
    from copilot.agent import loop

    return loop


def registry_mod() -> Any:
    from copilot.agent import registry

    return registry


# ---------------------------------------------------------------------------
# Snapshot / registry fixtures
# ---------------------------------------------------------------------------


def make_ref(resource_type: str, resource_id: str) -> Any:
    return contracts.ResourceRef(
        resource_type=resource_type, resource_id=resource_id
    )


def make_snapshot(*, med_name: str = "penicillin") -> Any:
    """A complete T005 snapshot with one entry per category in ``coverage``."""
    return contracts.PatientSnapshotOutput(
        patient=contracts.PatientRecord(
            ref=make_ref("Patient", "pat-1"), name="Jane Doe"
        ),
        medications=(
            contracts.MedicationRecord(
                ref=make_ref("MedicationRequest", "mr-1"),
                medication=med_name,
                status="active",
                source="prescriptions",
            ),
        ),
        conditions=(),
        allergies=(),
        labs=(),
        last_encounter=None,
        coverage=(
            contracts.CoverageOk(category="demographics", record_count=1),
            contracts.CoverageOk(category="medications", record_count=1),
            contracts.CoverageVerifiedEmpty(
                category="problems",
                query_description="Condition?patient=pat-1",
                scope="all problems for patient pat-1",
                timestamp=AWARE,
            ),
            contracts.CoverageVerifiedEmpty(
                category="allergies",
                query_description="AllergyIntolerance?patient=pat-1",
                scope="all allergies for patient pat-1",
                timestamp=AWARE,
            ),
            contracts.CoverageVerifiedEmpty(
                category="labs",
                query_description="Observation?patient=pat-1&category=laboratory",
                scope="recent labs for patient pat-1",
                timestamp=AWARE,
            ),
            contracts.CoverageVerifiedEmpty(
                category="last_encounter",
                query_description="Encounter?patient=pat-1&_sort=-date&_count=1",
                scope="most recent encounter for patient pat-1",
                timestamp=AWARE,
            ),
        ),
    )


def snapshot_only_registry(snapshot: Any) -> ToolRegistry:
    """A registry whose single ``get_patient_snapshot`` tool returns ``snapshot``."""

    async def executor(validated: Any) -> Any:
        assert isinstance(validated, GetPatientSnapshotInput)
        return snapshot

    return ToolRegistry(
        [
            Tool(
                name="get_patient_snapshot",
                description="Fetch this patient's snapshot in one parallel call.",
                input_model=GetPatientSnapshotInput,
                executor=executor,
            )
        ]
    )


# ---------------------------------------------------------------------------
# Scripted fake LLM clients (implement the LLMClient port; some raise)
# ---------------------------------------------------------------------------


class UnavailableLLM:
    """Raises a typed unavailability error on every ``complete`` call."""

    def __init__(self, exc_factory: Any) -> None:
        self._exc_factory = exc_factory
        self.calls: list[SimpleNamespace] = []

    async def complete(self, *, system: str, messages: Any, tools: Any) -> Any:
        self.calls.append(SimpleNamespace(messages=list(messages)))
        raise self._exc_factory()

    @property
    def call_count(self) -> int:
        return len(self.calls)


class HangingLLM:
    """Sleeps past any sane deadline; records whether it ever completed."""

    def __init__(self, hang: float) -> None:
        self._hang = hang
        self.completed = False
        self.calls = 0

    async def complete(self, *, system: str, messages: Any, tools: Any) -> Any:
        import asyncio

        self.calls += 1
        await asyncio.sleep(self._hang)
        self.completed = True
        return ports.LLMResponse(
            stop_reason=ports.StopReason.END_TURN, text="never used"
        )


class ProgrammableLLM:
    """Each scripted item is an ``LLMResponse`` to return or an exception to raise."""

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.calls: list[SimpleNamespace] = []

    async def complete(self, *, system: str, messages: Any, tools: Any) -> Any:
        self.calls.append(
            SimpleNamespace(system=system, messages=list(messages), tools=list(tools))
        )
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    @property
    def call_count(self) -> int:
        return len(self.calls)


class ToolThenFailLLM:
    """Returns a tool-use turn, then raises — recording the transport count
    at the instant of failure so a post-hoc refetch is detectable."""

    def __init__(self, tool_response: Any, seen: list[Any], exc_factory: Any) -> None:
        self._tool_response = tool_response
        self._seen = seen
        self._exc_factory = exc_factory
        self.calls = 0
        self.count_at_failure: int | None = None

    async def complete(self, *, system: str, messages: Any, tools: Any) -> Any:
        self.calls += 1
        if self.calls == 1:
            return self._tool_response
        self.count_at_failure = len(self._seen)
        raise self._exc_factory()


# ---------------------------------------------------------------------------
# Response / request builders
# ---------------------------------------------------------------------------


def final(text: str | None) -> Any:
    return ports.LLMResponse(stop_reason=ports.StopReason.END_TURN, text=text)


def refusal() -> Any:
    return ports.LLMResponse(stop_reason=ports.StopReason.REFUSAL)


def tool_use_snapshot(patient_id: str = "pat-1") -> Any:
    return ports.LLMResponse(
        stop_reason=ports.StopReason.TOOL_USE,
        tool_calls=(
            ports.ToolCallRequest(
                id="tc-1",
                name="get_patient_snapshot",
                arguments={"patient_id": patient_id},
            ),
        ),
    )


def spy_transport(seen: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json={"resourceType": "Bundle", "type": "searchset"}
        )

    return httpx.MockTransport(handler)


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


def client_for(
    llm: Any,
    registry: ToolRegistry,
    *,
    store: Any | None = None,
    llm_timeout: float | None = None,
) -> TestClient:
    kwargs: dict[str, Any] = {
        "chat_llm": llm,
        "chat_registry_factory": lambda token: registry,
    }
    if store is not None:
        kwargs["conversation_store"] = store
    if llm_timeout is not None:
        kwargs["chat_llm_timeout"] = llm_timeout
    return TestClient(create_app(**kwargs))


def build_loop(llm: Any, registry: ToolRegistry, **kwargs: Any) -> Any:
    return loop_mod().AgentLoop(
        llm=llm,
        registry=registry,
        patient_id=kwargs.pop("patient_id", "pat-1"),
        model="claude-opus-4-8",
        correlation_id="corr-1",
        **kwargs,
    )


# ==========================================================================
# Error taxonomy — pure, on the port, no vendor types
# ==========================================================================


def test_llm_error_taxonomy_hierarchy() -> None:
    assert issubclass(ports.LLMUnavailable, Exception)
    assert issubclass(ports.LLMTimeout, ports.LLMUnavailable)
    assert issubclass(ports.LLMTransportError, ports.LLMUnavailable)
    # Distinct concrete types, not aliases of one another.
    assert ports.LLMTimeout is not ports.LLMTransportError


def test_ports_and_loop_import_purity_still_holds() -> None:
    """Trap 5: the new taxonomy must not pull anthropic/httpx into the port."""
    code = (
        "import sys\n"
        "import copilot.agent.loop, copilot.agent.ports\n"
        "bad = [m for m in ('anthropic', 'httpx') if m in sys.modules]\n"
        "assert not bad, 'leaked: ' + repr(bad)\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "OK" in result.stdout


# ==========================================================================
# Loop-level — timeout enforcement, degraded result, axis exclusivity
# ==========================================================================


async def test_loop_abandons_hung_llm_call_at_the_deadline() -> None:
    """Criterion 2: a hung call is abandoned; wall-clock bounded far below hang."""
    import asyncio

    llm = HangingLLM(hang=5.0)
    loop = build_loop(llm, snapshot_only_registry(make_snapshot()), llm_timeout=0.05)

    t0 = time.monotonic()
    result = await loop.run("Catch me up.")
    elapsed = time.monotonic() - t0

    assert elapsed < 1.0, elapsed
    assert result.is_degraded
    assert result.fallback is None
    assert result.snapshot is None  # nothing ran before the hang was cut off
    # The hung coroutine was cancelled, not awaited to completion.
    await asyncio.sleep(0.2)
    assert llm.completed is False


async def test_loop_returns_degraded_on_immediate_unavailability() -> None:
    llm = UnavailableLLM(lambda: ports.LLMUnavailable("provider down"))
    loop = build_loop(llm, snapshot_only_registry(make_snapshot()))
    result = await loop.run("Catch me up.")

    assert result.is_degraded
    assert result.fallback is None  # degraded and FallbackReason are exclusive
    assert result.snapshot is None
    assert llm.call_count == 1


async def test_loop_captures_snapshot_when_failure_follows_a_tool_run() -> None:
    """Criterion 3 (loop level): the already-fetched snapshot is retained."""
    snap = make_snapshot()
    llm = ProgrammableLLM(
        [tool_use_snapshot(), ports.LLMUnavailable("provider down")]
    )
    loop = build_loop(llm, snapshot_only_registry(snap))
    result = await loop.run("Catch me up.")

    assert result.is_degraded
    assert result.snapshot == snap  # reused, not None
    assert result.fallback is None


async def test_refusal_fallback_carries_no_degraded_marker() -> None:
    """A T010 FallbackReason outcome is not degraded."""
    llm = ProgrammableLLM([refusal()])
    loop = build_loop(llm, snapshot_only_registry(make_snapshot()))
    result = await loop.run("What should I prescribe?")

    assert result.is_fallback
    assert result.fallback.reason == loop_mod().FallbackReason.REFUSAL
    assert result.degraded is None  # the other axis is untouched


# ==========================================================================
# Criterion 1 (mandatory) — first-turn unavailability -> degraded snapshot
# ==========================================================================


def test_first_turn_llm_unavailable_returns_degraded_snapshot() -> None:
    registry = snapshot_only_registry(make_snapshot())
    llm = UnavailableLLM(lambda: ports.LLMUnavailable("provider down"))
    client = client_for(llm, registry)

    resp = client.post("/chat", json=chat_body())
    assert resp.status_code == 200
    body = resp.json()

    assert body["degraded"] == "llm_unavailable"
    assert body["fallback"] is False
    assert body["snapshot"] is not None
    categories = [c["category"] for c in body["snapshot"]["coverage"]]
    assert set(categories) == set(EXPECTED_CATEGORIES)
    assert len(categories) == 6  # coverage intact — one entry per category

    counts = body["verification"]
    assert (
        counts["claims_total"],
        counts["claims_passed"],
        counts["claims_stripped"],
    ) == (0, 0, 0)


# ==========================================================================
# Criterion 2 (mandatory) — hang past the deadline -> fast degraded response
# ==========================================================================


def test_hanging_llm_times_out_fast_and_returns_degraded_snapshot() -> None:
    registry = snapshot_only_registry(make_snapshot())
    llm = HangingLLM(hang=5.0)
    client = client_for(llm, registry, llm_timeout=0.05)

    t0 = time.monotonic()
    resp = client.post("/chat", json=chat_body())
    elapsed = time.monotonic() - t0

    assert resp.status_code == 200
    assert resp.json()["degraded"] == "llm_unavailable"
    assert resp.json()["snapshot"] is not None
    # Bound the wall-clock: a status-only assertion would pass even if the loop
    # awaited the 5s hang to completion before falling back.
    assert elapsed < 1.0, elapsed


# ==========================================================================
# Criterion 3 (mandatory, most-faked) — reuse snapshot, do NOT refetch
# ==========================================================================


def test_failure_after_tool_run_reuses_snapshot_without_refetch() -> None:
    seen: list[httpx.Request] = []
    fhir_client = FhirClient(BASE_URL, TOKEN, transport=spy_transport(seen))
    registry = registry_mod().build_default_registry(fhir_client)
    llm = ToolThenFailLLM(
        tool_use_snapshot(),
        seen,
        lambda: ports.LLMUnavailable("provider down"),
    )
    client = client_for(llm, registry)

    resp = client.post("/chat", json=chat_body())
    assert resp.status_code == 200
    body = resp.json()
    assert body["degraded"] == "llm_unavailable"
    assert body["snapshot"] is not None

    # The snapshot tool actually reached the transport during the run ...
    assert seen, "the snapshot tool never reached the transport"
    assert llm.count_at_failure is not None
    # ... and NOT a single request was made after the LLM failed: the fallback
    # reused the already-fetched snapshot rather than refetching it.
    assert len(seen) == llm.count_at_failure, (
        len(seen),
        llm.count_at_failure,
        [str(r.url) for r in seen],
    )
    # No FHIR resource was fetched twice (a refetch would duplicate URLs).
    urls = [str(r.url) for r in seen]
    assert len(urls) == len(set(urls)), urls


# ==========================================================================
# Criterion 4 (mandatory) — follow-up during outage; recover on same conv
# ==========================================================================


def test_followup_during_outage_preserves_turns_and_recovers() -> None:
    from copilot.conversation import InMemoryConversationStore

    store = InMemoryConversationStore(ttl_seconds=3600)
    registry = snapshot_only_registry(make_snapshot())
    llm = ProgrammableLLM(
        [
            final("I reviewed the labs."),
            ports.LLMUnavailable("provider down"),
            final("I checked her medications."),
        ]
    )
    client = client_for(llm, registry, store=store)

    # Turn 1 — succeeds and is stored.
    r1 = client.post("/chat", json=chat_body("Catch me up."))
    assert r1.status_code == 200
    conv = r1.json()["conversation_id"]
    assert r1.json()["reply"] == "I reviewed the labs."
    stored_after_turn1 = store.get(conv).model_dump_json()

    # Turn 2 — outage. Degraded, no snapshot render, prior turns untouched.
    r2 = client.post(
        "/chat", json=chat_body("What about allergies?", conversation_id=conv)
    )
    assert r2.status_code == 200
    b2 = r2.json()
    assert b2["degraded"] == "llm_unavailable"
    assert b2["fallback"] is False
    assert b2.get("snapshot") is None  # follow-ups have no non-AI rendering
    assert "unavailable" in b2["reply"].lower()
    # The failed turn was not appended: prior turns are byte-identical.
    assert store.get(conv).model_dump_json() == stored_after_turn1

    # Turn 3 — recovery on the SAME conversation, using the preserved history.
    r3 = client.post(
        "/chat", json=chat_body("And her meds?", conversation_id=conv)
    )
    assert r3.status_code == 200
    assert r3.json()["reply"] == "I checked her medications."
    third_contents = [m.content for m in llm.calls[2].messages]
    assert any("Catch me up." in c for c in third_contents), third_contents
    assert any("I reviewed the labs." in c for c in third_contents), third_contents
    # The outage turn 2 was never recorded, so it is absent from the replay.
    assert not any("What about allergies?" in c for c in third_contents), third_contents


# ==========================================================================
# Criterion 5 axis-exclusivity (mandatory) — refusal at endpoint has no degraded
# ==========================================================================


def test_refusal_response_at_endpoint_has_no_degraded_marker() -> None:
    registry = snapshot_only_registry(make_snapshot())
    llm = ProgrammableLLM([refusal()])
    client = client_for(llm, registry)

    resp = client.post("/chat", json=chat_body("What should I prescribe?"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["fallback"] is True
    assert "degraded" not in body  # the degraded axis is unset (never emitted)
    assert "snapshot" not in body


# ==========================================================================
# Mandatory — no traceback / exception class name / module path leaks
# ==========================================================================


def test_degraded_responses_never_leak_tracebacks_or_class_names() -> None:
    registry = snapshot_only_registry(make_snapshot())
    factories = [
        lambda: ports.LLMUnavailable("boom"),
        lambda: ports.LLMTransportError("connection reset by peer"),
        lambda: ports.LLMTimeout("call exceeded deadline"),
    ]
    for factory in factories:
        client = client_for(UnavailableLLM(factory), registry)
        json_resp = client.post("/chat", json=chat_body())
        sse_resp = client.post(
            "/chat", params={"stream": "true"}, json=chat_body()
        )
        assert json_resp.status_code == 200
        assert sse_resp.status_code == 200
        for text in (json_resp.text, sse_resp.text):
            assert "Traceback" not in text
            assert "LLMUnavailable" not in text
            assert "LLMTransportError" not in text
            assert "LLMTimeout" not in text
            assert "copilot." not in text
            assert "provider down" not in text  # raw exception message hidden


# ==========================================================================
# Adversarial probes of my own devising (beyond the mandated list)
# ==========================================================================


def test_transport_error_also_degrades_with_a_snapshot() -> None:
    """My own: the taxonomy's third member (LLMTransportError) — not only the
    base and the timeout — must drive the identical degraded path."""
    registry = snapshot_only_registry(make_snapshot())
    llm = UnavailableLLM(lambda: ports.LLMTransportError("connection reset"))
    client = client_for(llm, registry)

    resp = client.post("/chat", json=chat_body())
    assert resp.status_code == 200
    body = resp.json()
    assert body["degraded"] == "llm_unavailable"
    assert body["fallback"] is False
    assert body["snapshot"] is not None


def test_hung_llm_coroutine_is_cancelled_not_awaited_to_completion() -> None:
    """My own: prove cancellation, not just a fast return. If the loop awaited
    the hung coroutine to completion in the background, ``completed`` would flip."""
    registry = snapshot_only_registry(make_snapshot())
    llm = HangingLLM(hang=2.0)
    client = client_for(llm, registry, llm_timeout=0.05)

    resp = client.post("/chat", json=chat_body())
    assert resp.status_code == 200
    assert resp.json()["degraded"] == "llm_unavailable"
    assert llm.completed is False


def test_snapshot_clinical_content_is_not_run_through_verification() -> None:
    """My own: structured tool data is never fed to verify_response. A med name
    that reads like an uncited clinical claim survives verbatim in the payload —
    the claim verifier would have stripped it. Counts stay all zero."""
    registry = snapshot_only_registry(make_snapshot(med_name="penicillin"))
    llm = UnavailableLLM(lambda: ports.LLMUnavailable("provider down"))
    client = client_for(llm, registry)

    resp = client.post("/chat", json=chat_body())
    assert resp.status_code == 200
    assert "penicillin" in resp.text  # not stripped -> never verified
    counts = resp.json()["verification"]
    assert (
        counts["claims_total"],
        counts["claims_passed"],
        counts["claims_stripped"],
    ) == (0, 0, 0)


def test_normal_answer_path_is_untouched_by_the_fallback_wiring() -> None:
    """My own regression guard: a healthy LLM turn still verifies and answers
    with no degraded marker and no snapshot leaking into the body."""
    registry = snapshot_only_registry(make_snapshot())
    llm = ProgrammableLLM([final("I reviewed the labs.")])
    client = client_for(llm, registry)

    resp = client.post("/chat", json=chat_body())
    assert resp.status_code == 200
    body = resp.json()
    assert body["reply"] == "I reviewed the labs."
    assert body["fallback"] is False
    assert "degraded" not in body
    assert "snapshot" not in body
    assert set(body.keys()) == {
        "conversation_id",
        "correlation_id",
        "reply",
        "verification",
        "fallback",
    }
