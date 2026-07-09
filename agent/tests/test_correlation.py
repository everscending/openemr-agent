"""Tests for correlation-ID middleware, accessor, and logging (T001 criteria 3-4)."""

from __future__ import annotations

import logging
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from copilot.app import create_app
from copilot.correlation import get_correlation_id

HEADER = "X-Correlation-ID"


def _app_with_probe_routes() -> FastAPI:
    """Production app plus test-only routes that expose request-scoped state."""
    app = create_app()

    @app.get("/_test/correlation-id")
    def read_correlation_id() -> dict[str, str | None]:
        return {"correlation_id": get_correlation_id()}

    @app.get("/_test/log")
    def emit_log() -> dict[str, str]:
        logging.getLogger("copilot.tests.probe").info("probe log entry")
        return {"logged": "yes"}

    return app


# --- Criterion 3: every response carries X-Correlation-ID -------------------


def test_response_has_generated_uuid4_correlation_id_when_none_supplied() -> None:
    client = TestClient(create_app())
    response = client.get("/health")
    assert HEADER in response.headers
    value = response.headers[HEADER]
    parsed = uuid.UUID(value)  # raises ValueError if not a valid UUID
    assert parsed.version == 4
    assert str(parsed) == value.lower()


def test_generated_correlation_ids_are_fresh_per_request() -> None:
    client = TestClient(create_app())
    first = client.get("/health").headers[HEADER]
    second = client.get("/health").headers[HEADER]
    assert first != second


def test_supplied_correlation_id_is_echoed_back_exactly() -> None:
    client = TestClient(create_app())
    supplied = "my-custom-correlation-id-42"
    response = client.get("/health", headers={HEADER: supplied})
    assert response.headers[HEADER] == supplied


def test_correlation_id_header_present_on_error_responses_too() -> None:
    client = TestClient(create_app())
    response = client.get("/no-such-route-anywhere")
    assert response.status_code == 404
    assert HEADER in response.headers


# --- Criterion 4: request-scoped accessor + structured logging --------------


def test_handler_sees_same_correlation_id_via_accessor() -> None:
    client = TestClient(_app_with_probe_routes())
    supplied = str(uuid.uuid4())
    response = client.get("/_test/correlation-id", headers={HEADER: supplied})
    assert response.json() == {"correlation_id": supplied}
    assert response.headers[HEADER] == supplied


def test_accessor_returns_generated_id_when_none_supplied() -> None:
    client = TestClient(_app_with_probe_routes())
    response = client.get("/_test/correlation-id")
    body_id = response.json()["correlation_id"]
    assert body_id is not None
    assert body_id == response.headers[HEADER]
    assert uuid.UUID(body_id).version == 4


def test_log_records_carry_correlation_id_as_structured_field(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = TestClient(_app_with_probe_routes())
    supplied = str(uuid.uuid4())
    with caplog.at_level(logging.INFO, logger="copilot.tests.probe"):
        client.get("/_test/log", headers={HEADER: supplied})
    probe_records = [r for r in caplog.records if r.name == "copilot.tests.probe"]
    assert probe_records, "expected the probe route to emit a log record"
    record = probe_records[-1]
    # Structured field on the LogRecord itself — not string-matched output.
    assert getattr(record, "correlation_id", None) == supplied
