"""FastAPI application factory for the Clinical Co-Pilot agent service."""

from __future__ import annotations

from fastapi import FastAPI

from copilot.correlation import (
    CorrelationIdMiddleware,
    install_correlation_log_record_factory,
)


def create_app() -> FastAPI:
    """Build and return the Co-Pilot FastAPI application."""
    install_correlation_log_record_factory()

    app = FastAPI(title="Clinical Co-Pilot Agent")
    app.add_middleware(CorrelationIdMiddleware)

    @app.get("/health")
    def health() -> dict[str, str]:
        """Liveness probe: no external calls, process-up check only."""
        return {"status": "ok"}

    return app


app = create_app()
