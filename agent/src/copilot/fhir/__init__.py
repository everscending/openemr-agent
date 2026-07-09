"""Read-only FHIR client with user-token passthrough (T003).

The only path to patient data: every request carries the requesting user's
SMART token, exposes GET-based ``read``/``search`` only, and raises a typed
error taxonomy on failure.
"""

from copilot.fhir.client import FhirClient, SearchResult
from copilot.fhir.errors import (
    FhirAuthError,
    FhirError,
    FhirHttpStatusError,
    FhirMalformedResponse,
    FhirNotFound,
    FhirTimeout,
    FhirUpstreamError,
)

__all__ = [
    "FhirAuthError",
    "FhirClient",
    "FhirError",
    "FhirHttpStatusError",
    "FhirMalformedResponse",
    "FhirNotFound",
    "FhirTimeout",
    "FhirUpstreamError",
    "SearchResult",
]
