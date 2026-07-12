"""PHP -> agent -> FHIR correlation ID propagation (T046).

PRD.md:308-310 / ARCHITECTURE.md:65-67,99 require a correlation ID joinable
from logs alone across every service boundary the request crosses. T001
already made this true *inside* the agent process (see
``agent/tests/test_correlation.py``); ``agent/tests/test_chat.py`` and
``agent/tests/test_audit_bridge.py`` already prove the ``/chat`` endpoint
prefers a client-supplied ``X-Correlation-ID`` header over self-generating one
(e.g. ``test_audit_bridge.py::test_answered_turn_posts_audit_record_with_expected_fields``
uses a non-UUID-shaped fixed value and asserts it reaches the audit record
verbatim -- proof the agent never overrides it with its own mint).

What was still missing, and what this file covers:

  Criterion 3 -- the *outbound* FHIR calls the agent loop makes while serving
  a ``/chat`` turn must carry that SAME correlation ID as ``X-Correlation-ID``,
  proven end-to-end through the real production wiring (the ``/chat`` route,
  the real ``FhirClient._get`` code path) down to the mock HTTP transport --
  not by asserting a mock's own return value back to itself.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
from fastapi.testclient import TestClient

from copilot import contracts
from copilot.agent import ports
from copilot.agent.tools import Tool, ToolRegistry
from copilot.app import create_app
from copilot.correlation import CORRELATION_ID_HEADER
from copilot.fhir import FhirClient

FHIR_BASE_URL = "https://emr.example.test/apis/default/fhir"


class ScriptedLLM:
    """Returns canned responses in order (mirrors other test files' fake)."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)

    async def complete(self, *, system: str, messages: Any, tools: Any) -> Any:
        if not self._responses:
            raise AssertionError("LLM called more times than scripted")
        return self._responses.pop(0)


def final(text: str | None) -> Any:
    return ports.LLMResponse(stop_reason=ports.StopReason.END_TURN, text=text)


def tool_use(name: str, arguments: dict[str, Any], *, call_id: str = "tc-1") -> Any:
    return ports.LLMResponse(
        stop_reason=ports.StopReason.TOOL_USE,
        tool_calls=(ports.ToolCallRequest(id=call_id, name=name, arguments=arguments),),
    )


def observations_output() -> Any:
    return contracts.SearchObservationsOutput(
        records=(
            contracts.ObservationRecord(
                ref=contracts.ResourceRef(resource_type="Observation", resource_id="obs-1"),
                code="718-7",
                display="Hemoglobin",
                value="13.2 g/dL",
                effective=None,
            ),
        ),
        receipt=None,
    )


def fhir_backed_tool(seen: list[httpx.Request]) -> Tool:
    """A real tool whose executor makes a real outbound FHIR GET through a
    real FhirClient, over a MockTransport that records every request it
    receives -- the actual transport seam, not a stand-in for one."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "resourceType": "Bundle",
                "type": "searchset",
                "link": [{"relation": "self", "url": f"{FHIR_BASE_URL}/Observation"}],
            },
        )

    async def executor(validated: Any) -> Any:
        async with FhirClient(
            FHIR_BASE_URL, token="user-token-abc", transport=httpx.MockTransport(handler)
        ) as client:
            await client.search("Observation")
        return observations_output()

    return Tool(
        name="search_observations",
        description="A real FHIR-backed tool for T046 correlation-id boundary tests.",
        input_model=contracts.SearchObservationsInput,
        executor=executor,
    )


def make_client(seen: list[httpx.Request]) -> TestClient:
    tool = fhir_backed_tool(seen)
    llm = ScriptedLLM(
        [
            tool_use("search_observations", {}),
            final("The latest hemoglobin is 13.2 g/dL."),
        ]
    )
    kwargs: dict[str, Any] = {
        "chat_llm": llm,
        "chat_registry_factory": lambda token: ToolRegistry((tool,)),
    }
    return TestClient(create_app(**kwargs))


def chat_body() -> dict[str, Any]:
    return {
        "message": "What's the latest hemoglobin?",
        "patient_id": "pat-1",
        "token": "user-token-abc",
    }


def test_outbound_fhir_call_carries_the_php_originated_correlation_id() -> None:
    """The whole point: a distinctive ID that arrives on the /chat request
    (standing in for the PHP-minted UUIDv4 -- proven separately, PHP-side, in
    tests/Tests/Isolated/Modules/ClinicalCopilot/CorrelationIdBoundaryTest.php)
    must reach the FHIR transport seam byte-identical."""
    distinctive = "corr-e2e-f47ac10b-58cc"
    seen: list[httpx.Request] = []
    client = make_client(seen)

    resp = client.post(
        "/chat", json=chat_body(), headers={CORRELATION_ID_HEADER: distinctive}
    )

    assert resp.status_code == 200
    assert resp.json()["correlation_id"] == distinctive
    assert len(seen) == 1, "expected exactly one outbound FHIR request from the tool call"
    assert seen[0].headers[CORRELATION_ID_HEADER.lower()] == distinctive


def test_two_chat_requests_forward_two_distinct_correlation_ids_to_fhir() -> None:
    """Adversarial: proves the FHIR call tracks whichever request is actually
    in flight, not a value fixed at app/tool construction time."""
    seen: list[httpx.Request] = []
    client = make_client(seen)

    first = client.post(
        "/chat", json=chat_body(), headers={CORRELATION_ID_HEADER: "corr-first-111"}
    )
    assert first.status_code == 200

    seen2: list[httpx.Request] = []
    client2 = make_client(seen2)
    second = client2.post(
        "/chat", json=chat_body(), headers={CORRELATION_ID_HEADER: "corr-second-222"}
    )
    assert second.status_code == 200

    assert seen[0].headers[CORRELATION_ID_HEADER.lower()] == "corr-first-111"
    assert seen2[0].headers[CORRELATION_ID_HEADER.lower()] == "corr-second-222"
