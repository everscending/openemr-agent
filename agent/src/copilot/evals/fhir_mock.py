"""Build an ``httpx.MockTransport`` from an eval case's fixture data (T015).

No network, ever (ARCHITECTURE.md section 8 design decisions): every FHIR
read/search a case's scenario touches is served from the case's own
``fhir_fixture``/``fhir_failures`` data, never a real socket.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

import httpx

from copilot.contracts.refs import FhirResourceType

#: Every FHIR resource type any tool in this service reads — used to locate
#: the resource-type path segment regardless of the base URL's own path
#: prefix (e.g. ``/apis/default/fhir``), so fixture completeness never
#: matters for routing.
_KNOWN_RESOURCE_TYPES: frozenset[str] = frozenset(t.value for t in FhirResourceType)

_NOT_FOUND_BODY: dict[str, Any] = {
    "resourceType": "OperationOutcome",
    "issue": [{"severity": "error", "code": "not-found"}],
}
_UPSTREAM_ERROR_BODY: dict[str, Any] = {
    "resourceType": "OperationOutcome",
    "issue": [{"severity": "error", "code": "exception"}],
}


def build_fhir_mock_transport(
    fixture: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    failing_resource_types: Sequence[str] = (),
) -> httpx.MockTransport:
    """A mock transport serving ``fixture`` for reads and searches.

    A ``read`` (``GET /{Type}/{id}``) returns the matching fixture resource,
    or a 404 if the type or id is absent — a genuine "not found", not a
    silently-empty result. A ``search`` (``GET /{Type}``) returns a Bundle of
    every fixture resource registered under that type (query parameters are
    not filtered — this is a deterministic fixture server, not a FHIR search
    engine); a type with no fixture entries at all still returns a valid,
    empty Bundle (never a 404) since a search legitimately can come back
    empty. Any resource type named in ``failing_resource_types`` always
    answers with a 500 (upstream failure), regardless of fixture data — for
    provoking a genuine, typed tool failure.
    """
    failing = frozenset(failing_resource_types)

    def handler(request: httpx.Request) -> httpx.Response:
        segments = [s for s in request.url.path.split("/") if s]
        resource_type: str | None = None
        type_index: int | None = None
        for index, segment in enumerate(segments):
            if segment in _KNOWN_RESOURCE_TYPES:
                resource_type = segment
                type_index = index
                break

        if resource_type is None:
            return httpx.Response(404, json=_NOT_FOUND_BODY)

        if resource_type in failing:
            return httpx.Response(500, json=_UPSTREAM_ERROR_BODY)

        assert type_index is not None
        remainder = segments[type_index + 1 :]
        resource_id = remainder[0] if remainder else None
        records = list(fixture.get(resource_type, ()))

        if resource_id is not None:
            match = next(
                (r for r in records if r.get("id") == resource_id), None
            )
            if match is None:
                return httpx.Response(404, json=_NOT_FOUND_BODY)
            return httpx.Response(200, json=dict(match))

        return httpx.Response(
            200,
            json={
                "resourceType": "Bundle",
                "type": "searchset",
                "entry": [{"resource": dict(r)} for r in records],
            },
        )

    return httpx.MockTransport(handler)
