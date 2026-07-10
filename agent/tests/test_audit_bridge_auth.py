"""T016 criterion 7 / mandatory adversarial #12 — the agent side sends the
acting user's bearer token to the audit bridge, the raw token never leaks, and
the fail-open property still holds byte-identically with the bridge 401ing.

The outgoing ``Authorization`` header is asserted at the mock-transport seam
(not by inspecting the record body, which by construction carries only the
sha256 *hash* of the token). These tests live in a NEW file; the T013 locked
suite (``test_audit_bridge.py``) is never edited.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from copilot.agent import ports
from copilot.agent.tools import ToolRegistry
from copilot.app import create_app
from copilot.conversation import ConversationRecord, InMemoryConversationStore, hash_token
from copilot.correlation import CORRELATION_ID_HEADER

BRIDGE_URL = "https://openemr.example.test/apis/default/copilot/audit-bridge"

RAW_TOKEN = "SUPER-SECRET-USER-BEARER-abc123"
FIXED_CONV_ID = "conv-fixed-auth-0001"
FIXED_CORR_ID = "corr-fixed-auth-0001"


class ScriptedLLM:
    async def complete(self, *, system: str, messages: Any, tools: Any) -> Any:
        return ports.LLMResponse(stop_reason=ports.StopReason.END_TURN, text="Reviewed.")


def empty_registry() -> ToolRegistry:
    return ToolRegistry([])


class FakeMetricsRecorder:
    def __init__(self) -> None:
        self.successes = 0
        self.failures = 0

    def record_success(self) -> None:
        self.successes += 1

    def record_failure(self) -> None:
        self.failures += 1


def capturing_transport(
    captured: list[httpx.Request], status: int = 200
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(status, json={"status": "recorded"})

    return httpx.MockTransport(handler)


def bridge_client(
    transport: httpx.AsyncBaseTransport,
    *,
    metrics: Any | None = None,
    logger: logging.Logger | None = None,
) -> Any:
    import copilot.audit as audit

    return audit.AuditBridgeClient(
        base_url=BRIDGE_URL,
        transport=transport,
        metrics=metrics if metrics is not None else FakeMetricsRecorder(),
        timeout=2.0,
        logger=logger,
    )


def seeded_store() -> InMemoryConversationStore:
    store = InMemoryConversationStore(ttl_seconds=3600)
    store.put(
        FIXED_CONV_ID,
        ConversationRecord(patient_id="pat-1", user_token_hash=hash_token(RAW_TOKEN)),
    )
    return store


def make_client(audit_bridge: Any, *, store: Any | None = None) -> TestClient:
    return TestClient(
        create_app(
            chat_llm=ScriptedLLM(),
            chat_registry_factory=lambda token: empty_registry(),
            conversation_store=store,
            audit_bridge=audit_bridge,
        )
    )


def post(client: TestClient, *, stream: bool = False) -> httpx.Response:
    params = {"stream": "true"} if stream else {}
    return client.post(
        "/chat",
        params=params,
        headers={CORRELATION_ID_HEADER: FIXED_CORR_ID},
        json={
            "message": "does she still take penicillin",
            "patient_id": "pat-1",
            "token": RAW_TOKEN,
            "conversation_id": FIXED_CONV_ID,
        },
    )


def test_bridge_post_carries_the_acting_users_bearer_token() -> None:
    captured: list[httpx.Request] = []
    client = make_client(bridge_client(capturing_transport(captured)), store=seeded_store())

    resp = post(client)
    assert resp.status_code == 200
    assert len(captured) == 1

    # Asserted at the transport seam, not from the record body.
    assert captured[0].headers.get("Authorization") == f"Bearer {RAW_TOKEN}"
    assert captured[0].headers.get("Content-Type") == "application/json"


def test_raw_token_never_appears_in_body_or_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    captured: list[httpx.Request] = []
    logger = logging.getLogger("copilot.tests.audit.auth.leak")
    metrics = FakeMetricsRecorder()
    client = make_client(
        bridge_client(capturing_transport(captured), metrics=metrics, logger=logger),
        store=seeded_store(),
    )

    with caplog.at_level(logging.DEBUG):
        resp = post(client)
    assert resp.status_code == 200
    assert len(captured) == 1

    # The serialized record body carries only the hash, never the raw token.
    body = captured[0].content.decode("utf-8")
    assert RAW_TOKEN not in body
    assert hash_token(RAW_TOKEN) in body

    # No emitted log record anywhere carries the raw token.
    for record in caplog.records:
        assert RAW_TOKEN not in record.getMessage()
        for value in vars(record).values():
            assert RAW_TOKEN != value
            if isinstance(value, str):
                assert RAW_TOKEN not in value


def test_missing_token_does_not_post_but_still_counts_one_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An empty user token must not POST (a record it cannot bind), yet delivery
    still fails-open: one failure counted, one alert, never a raise."""
    import copilot.audit as audit

    captured: list[httpx.Request] = []
    metrics = FakeMetricsRecorder()
    logger = logging.getLogger("copilot.tests.audit.auth.missing")
    client_obj = audit.AuditBridgeClient(
        base_url=BRIDGE_URL,
        transport=capturing_transport(captured),
        metrics=metrics,
        timeout=2.0,
        logger=logger,
    )

    record = audit.AuditInvocationRecord(
        user_token_hash=hash_token(RAW_TOKEN),
        patient_id="pat-1",
        correlation_id=FIXED_CORR_ID,
        conversation_id=FIXED_CONV_ID,
        occurred_at="2026-07-09T12:00:00Z",
        claims_total=0,
        claims_passed=0,
        claims_stripped=0,
        outcome=audit.AuditOutcome.ANSWERED,
    )

    import asyncio

    with caplog.at_level(logging.ERROR, logger=logger.name):
        asyncio.run(client_obj.deliver(record, user_token=""))

    assert captured == []  # never POSTed a record it cannot attribute
    assert metrics.failures == 1
    assert metrics.successes == 0
    alerts = [r for r in caplog.records if r.name == logger.name]
    assert len(alerts) == 1


def test_bridge_401_leaves_chat_json_response_byte_identical() -> None:
    healthy_captured: list[httpx.Request] = []
    healthy = make_client(
        bridge_client(capturing_transport(healthy_captured, status=200)), store=seeded_store()
    )
    healthy_resp = post(healthy)

    failing = make_client(
        bridge_client(capturing_transport([], status=401)), store=seeded_store()
    )
    failing_resp = post(failing)

    assert healthy_resp.status_code == failing_resp.status_code == 200
    assert failing_resp.content == healthy_resp.content
    # The healthy path did send the bearer (guards against the token-threading
    # change regressing while still returning 200).
    assert healthy_captured[0].headers.get("Authorization") == f"Bearer {RAW_TOKEN}"


def test_bridge_401_leaves_chat_sse_stream_byte_identical() -> None:
    healthy_captured: list[httpx.Request] = []
    healthy = make_client(
        bridge_client(capturing_transport(healthy_captured, status=200)), store=seeded_store()
    )
    healthy_resp = post(healthy, stream=True)

    failing = make_client(
        bridge_client(capturing_transport([], status=401)), store=seeded_store()
    )
    failing_resp = post(failing, stream=True)

    assert healthy_resp.status_code == failing_resp.status_code == 200
    assert "event: verdict" in healthy_resp.text
    assert failing_resp.text == healthy_resp.text
    assert healthy_captured[0].headers.get("Authorization") == f"Bearer {RAW_TOKEN}"
