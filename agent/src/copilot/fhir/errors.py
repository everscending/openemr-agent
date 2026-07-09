"""Typed error taxonomy for the read-only FHIR client (T003 criterion 3).

Every failure carries the attempted resource type and correlation-relevant
context (URL, HTTP status where applicable) so tool failures are typed and
surfaced, never silent (ARCHITECTURE.md section 7).
"""

from __future__ import annotations


class FhirError(Exception):
    """Base class for all FHIR client failures."""

    def __init__(
        self,
        message: str,
        *,
        resource_type: str,
        url: str | None = None,
    ) -> None:
        super().__init__(message)
        self.resource_type = resource_type
        self.url = url


class FhirHttpStatusError(FhirError):
    """A FHIR failure tied to a specific upstream HTTP status code."""

    def __init__(
        self,
        message: str,
        *,
        resource_type: str,
        status_code: int,
        url: str | None = None,
    ) -> None:
        super().__init__(message, resource_type=resource_type, url=url)
        self.status_code = status_code


class FhirAuthError(FhirHttpStatusError):
    """The user's token was rejected by OpenEMR (401/403)."""


class FhirNotFound(FhirHttpStatusError):
    """The requested resource does not exist (404)."""


class FhirUpstreamError(FhirHttpStatusError):
    """OpenEMR failed server-side (5xx or otherwise unexpected status)."""


class FhirTimeout(FhirError):
    """The request exceeded the configured per-request timeout."""


class FhirMalformedResponse(FhirError):
    """The response body was not JSON, or not a valid Bundle/resource shape."""
