"""FastAPI application factory for the Clinical Co-Pilot agent service."""

from __future__ import annotations

from typing import Mapping

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from copilot.correlation import (
    CorrelationIdMiddleware,
    install_correlation_log_record_factory,
)
from copilot.readiness import (
    Checker,
    default_checkers,
    default_deadline,
    run_readiness_checks,
)


def create_app(
    readiness_checkers: Mapping[str, Checker] | None = None,
    readiness_deadline: float | None = None,
) -> FastAPI:
    """Build and return the Co-Pilot FastAPI application.

    ``readiness_checkers`` maps dependency keys to async checkers (return on
    success, raise on failure) probed by ``/ready``; omitted, the production
    reachability checkers are used. ``readiness_deadline`` bounds the overall
    probe time in seconds (default 5s, env-overridable via
    ``READINESS_DEADLINE_SECONDS``).
    """
    install_correlation_log_record_factory()

    checkers: Mapping[str, Checker] = (
        readiness_checkers if readiness_checkers is not None else default_checkers()
    )
    deadline = readiness_deadline if readiness_deadline is not None else default_deadline()

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

    return app


app = create_app()
