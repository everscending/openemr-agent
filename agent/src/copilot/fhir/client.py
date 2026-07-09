"""Async, read-only FHIR client with user-token passthrough (T003).

The agent holds no standing credentials: every request presents the
requesting user's SMART token so OpenEMR's ACL and scopes enforce access
(ARCHITECTURE.md section 4, trust boundary 2). The client exposes only
GET-based ``read`` and ``search`` operations and raises the typed error
taxonomy in :mod:`copilot.fhir.errors` on every failure.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Mapping

import httpx

from copilot.fhir.errors import (
    FhirAuthError,
    FhirMalformedResponse,
    FhirNotFound,
    FhirTimeout,
    FhirUpstreamError,
)

DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_PAGES = 10


@dataclass(frozen=True, slots=True)
class SearchResult:
    """Combined entries from a paginated FHIR search.

    ``truncated`` is True when the configured page cap was reached while a
    further ``next`` link remained — never silently dropped.
    """

    entries: list[dict[str, Any]]
    truncated: bool
    pages_fetched: int


class FhirClient:
    """Read-only async client for an OpenEMR FHIR endpoint.

    The bearer token is bound to the client instance at construction and is
    never read from module-level or global state.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_pages: int = DEFAULT_MAX_PAGES,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if max_pages < 1:
            raise ValueError("max_pages must be at least 1")
        self._timeout = timeout
        self._max_pages = max_pages
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/fhir+json",
            },
            timeout=httpx.Timeout(timeout),
            transport=transport,
        )

    async def __aenter__(self) -> "FhirClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Release the underlying HTTP connection pool."""
        await self._client.aclose()

    async def read(self, resource_type: str, resource_id: str) -> dict[str, Any]:
        """GET a single resource: ``{base}/{resource_type}/{resource_id}``."""
        payload = await self._get(
            f"/{resource_type}/{resource_id}", resource_type=resource_type
        )
        if not isinstance(payload, dict) or "resourceType" not in payload:
            raise FhirMalformedResponse(
                f"Response for {resource_type}/{resource_id} is not a FHIR resource",
                resource_type=resource_type,
            )
        return payload

    async def capability_statement(self) -> dict[str, Any]:
        """GET the server CapabilityStatement: ``{base}/metadata``.

        Used as a lightweight reachability probe (T004 readiness checks).
        """
        payload = await self._get("/metadata", resource_type="CapabilityStatement")
        if not isinstance(payload, dict) or "resourceType" not in payload:
            raise FhirMalformedResponse(
                "Response for metadata is not a FHIR resource",
                resource_type="CapabilityStatement",
            )
        return payload

    async def search(
        self,
        resource_type: str,
        params: Mapping[str, str] | None = None,
    ) -> SearchResult:
        """GET a search Bundle, transparently following ``next`` links.

        Follows pagination up to the configured page cap; hitting the cap
        with a ``next`` link still pending is surfaced via
        ``SearchResult.truncated``.
        """
        entries: list[dict[str, Any]] = []
        next_url: str | None = f"/{resource_type}"
        request_params: Mapping[str, str] | None = params
        pages_fetched = 0

        while next_url is not None:
            if pages_fetched >= self._max_pages:
                return SearchResult(
                    entries=entries, truncated=True, pages_fetched=pages_fetched
                )
            payload = await self._get(
                next_url, resource_type=resource_type, params=request_params
            )
            request_params = None  # `next` links embed their own query string
            pages_fetched += 1
            entries.extend(self._bundle_entries(payload, resource_type=resource_type))
            next_url = self._next_link(payload)

        return SearchResult(entries=entries, truncated=False, pages_fetched=pages_fetched)

    # -- internals ---------------------------------------------------------

    async def _get(
        self,
        url: str,
        *,
        resource_type: str,
        params: Mapping[str, str] | None = None,
    ) -> Any:
        try:
            async with asyncio.timeout(self._timeout):
                response = await self._client.get(url, params=params)
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise FhirTimeout(
                f"FHIR request for {resource_type} timed out "
                f"after {self._timeout}s",
                resource_type=resource_type,
                url=url,
            ) from exc

        request_url = str(response.request.url)
        status = response.status_code
        if status in (401, 403):
            raise FhirAuthError(
                f"FHIR request for {resource_type} was rejected ({status})",
                resource_type=resource_type,
                status_code=status,
                url=request_url,
            )
        if status == 404:
            raise FhirNotFound(
                f"FHIR resource {resource_type} not found",
                resource_type=resource_type,
                status_code=status,
                url=request_url,
            )
        if status < 200 or status >= 300:
            raise FhirUpstreamError(
                f"FHIR upstream failure for {resource_type} ({status})",
                resource_type=resource_type,
                status_code=status,
                url=request_url,
            )

        try:
            return response.json()
        except ValueError as exc:
            raise FhirMalformedResponse(
                f"FHIR response for {resource_type} is not valid JSON",
                resource_type=resource_type,
                url=request_url,
            ) from exc

    def _bundle_entries(
        self, payload: Any, *, resource_type: str
    ) -> list[dict[str, Any]]:
        if not isinstance(payload, dict) or payload.get("resourceType") != "Bundle":
            raise FhirMalformedResponse(
                f"Search response for {resource_type} is not a FHIR Bundle",
                resource_type=resource_type,
            )
        raw_entries = payload.get("entry", [])
        if not isinstance(raw_entries, list):
            raise FhirMalformedResponse(
                f"Bundle for {resource_type} has a malformed entry list",
                resource_type=resource_type,
            )
        resources: list[dict[str, Any]] = []
        for entry in raw_entries:
            if not isinstance(entry, dict) or not isinstance(
                entry.get("resource"), dict
            ):
                raise FhirMalformedResponse(
                    f"Bundle entry for {resource_type} lacks a resource",
                    resource_type=resource_type,
                )
            resources.append(entry["resource"])
        return resources

    @staticmethod
    def _next_link(payload: dict[str, Any]) -> str | None:
        links = payload.get("link", [])
        if not isinstance(links, list):
            return None
        for link in links:
            if isinstance(link, dict) and link.get("relation") == "next":
                url = link.get("url")
                if isinstance(url, str) and url:
                    return url
        return None
