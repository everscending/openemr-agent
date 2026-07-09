"""Tests for the /health liveness endpoint (T001 criterion 2)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from copilot.app import create_app


def test_health_returns_200_with_ok_body() -> None:
    """GET /health returns HTTP 200 and exactly {"status": "ok"}."""
    client = TestClient(create_app())
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
