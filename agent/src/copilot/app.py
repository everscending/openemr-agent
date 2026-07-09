"""FastAPI application factory for the Clinical Co-Pilot agent service.

Red-state baseline: bare app, no routes or middleware yet.
"""

from __future__ import annotations

from fastapi import FastAPI


def create_app() -> FastAPI:
    """Build and return the Co-Pilot FastAPI application."""
    return FastAPI(title="Clinical Co-Pilot Agent")


app = create_app()
