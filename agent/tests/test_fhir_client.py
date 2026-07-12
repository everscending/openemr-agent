"""Tests for the read-only FHIR client with user-token passthrough (T003).

Criteria map:
  1. Bearer token passthrough (per-instance, never global)
  2. Read-only API surface; only GET ever reaches the transport
  3. Typed error taxonomy carrying the attempted resource type
  4. Configurable per-request timeout -> FhirTimeout
  5. Bundle `next` pagination with a page cap surfaced as a truncated flag
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from copilot import correlation, fhir

BASE_URL = "https://emr.example.test/apis/default/fhir"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def json_response(payload: dict[str, Any], status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


def patient(resource_id: str) -> dict[str, Any]:
    return {"resourceType": "Patient", "id": resource_id}


def bundle(
    resources: list[dict[str, Any]], next_url: str | None = None
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "resourceType": "Bundle",
        "type": "searchset",
        "link": [{"relation": "self", "url": f"{BASE_URL}/Patient"}],
    }
    if resources:
        body["entry"] = [{"resource": r} for r in resources]
    if next_url is not None:
        body["link"].append({"relation": "next", "url": next_url})
    return body


def make_client(handler: Any, token: str = "test-token", **kwargs: Any) -> Any:
    return fhir.FhirClient(
        base_url=BASE_URL,
        token=token,
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def three_page_handler(seen: list[httpx.Request]) -> Any:
    """Handler serving 3 Bundle pages linked via `next` (p1..p5)."""

    pages = {
        "1": bundle(
            [patient("p1"), patient("p2")], next_url=f"{BASE_URL}/Patient?page=2"
        ),
        "2": bundle(
            [patient("p3"), patient("p4")], next_url=f"{BASE_URL}/Patient?page=3"
        ),
        "3": bundle([patient("p5")]),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        page = request.url.params.get("page", "1")
        return json_response(pages[page])

    return handler


# ---------------------------------------------------------------------------
# Criterion 1: bearer token passthrough, per-instance not global
# ---------------------------------------------------------------------------


async def test_read_sends_bearer_token_and_resource_url() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return json_response(patient("abc-123"))

    client = make_client(handler, token="user-token-1")
    resource = await client.read("Patient", "abc-123")

    assert resource["id"] == "abc-123"
    assert len(seen) == 1
    assert seen[0].headers["authorization"] == "Bearer user-token-1"
    assert str(seen[0].url) == f"{BASE_URL}/Patient/abc-123"


async def test_search_sends_bearer_token_and_query_params() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return json_response(bundle([patient("p1")]))

    client = make_client(handler, token="user-token-2")
    await client.search("Patient", {"name": "Smith"})

    assert len(seen) == 1
    assert seen[0].headers["authorization"] == "Bearer user-token-2"
    assert seen[0].url.params["name"] == "Smith"
    assert seen[0].url.path.endswith("/Patient")


async def test_token_is_per_client_instance_not_global() -> None:
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.headers["authorization"])
        return json_response(patient("p1"))

    transport = httpx.MockTransport(handler)
    client_alpha = fhir.FhirClient(
        base_url=BASE_URL, token="token-alpha", transport=transport
    )
    client_beta = fhir.FhirClient(
        base_url=BASE_URL, token="token-beta", transport=transport
    )

    # Interleave calls: each client must keep presenting its own token.
    await client_alpha.read("Patient", "p1")
    await client_beta.read("Patient", "p1")
    await client_alpha.read("Patient", "p1")

    assert captured == ["Bearer token-alpha", "Bearer token-beta", "Bearer token-alpha"]


async def test_pagination_requests_all_carry_the_token() -> None:
    seen: list[httpx.Request] = []
    client = make_client(three_page_handler(seen), token="paging-token")

    await client.search("Patient")

    assert len(seen) == 3
    assert all(r.headers["authorization"] == "Bearer paging-token" for r in seen)


# ---------------------------------------------------------------------------
# Criterion 2: read-only surface, GET only
# ---------------------------------------------------------------------------

WRITE_METHOD_NAMES = {
    "post",
    "put",
    "patch",
    "delete",
    "create",
    "update",
    "write",
    "insert",
    "remove",
    "send",
    "request",
}


def test_public_api_exposes_only_read_operations() -> None:
    public = {name for name in dir(fhir.FhirClient) if not name.startswith("_")}
    assert "read" in public
    assert "search" in public
    assert not (public & WRITE_METHOD_NAMES), (
        f"write-capable methods exposed: {public & WRITE_METHOD_NAMES}"
    )


async def test_only_get_requests_ever_reach_the_transport() -> None:
    methods: list[str] = []
    seen: list[httpx.Request] = []
    paging = three_page_handler(seen)

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method != "GET":
            return httpx.Response(405, json={"error": "non-GET rejected"})
        if "/Patient/" in request.url.path:
            return json_response(patient("p-read"))
        return paging(request)

    client = make_client(handler)
    await client.read("Patient", "p-read")
    await client.search("Patient")  # exercises pagination too

    assert methods, "no requests captured"
    assert set(methods) == {"GET"}
    assert len(methods) == 4  # 1 read + 3 bundle pages


# ---------------------------------------------------------------------------
# Criterion 3: typed error taxonomy carrying the attempted resource type
# ---------------------------------------------------------------------------


def test_error_taxonomy_shares_a_common_base() -> None:
    for exc_type in (
        fhir.FhirAuthError,
        fhir.FhirNotFound,
        fhir.FhirTimeout,
        fhir.FhirUpstreamError,
        fhir.FhirMalformedResponse,
    ):
        assert issubclass(exc_type, fhir.FhirError)
        assert issubclass(exc_type, Exception)


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_failures_raise_fhir_auth_error(status: int) -> None:
    client = make_client(
        lambda request: json_response({"error": "denied"}, status_code=status)
    )
    with pytest.raises(fhir.FhirAuthError) as exc_info:
        await client.read("Patient", "abc")
    assert exc_info.value.resource_type == "Patient"
    assert exc_info.value.status_code == status


async def test_404_raises_fhir_not_found() -> None:
    client = make_client(
        lambda request: json_response({"error": "gone"}, status_code=404)
    )
    with pytest.raises(fhir.FhirNotFound) as exc_info:
        await client.read("Condition", "missing-id")
    assert exc_info.value.resource_type == "Condition"
    assert exc_info.value.status_code == 404


@pytest.mark.parametrize("status", [500, 502, 503])
async def test_5xx_raises_fhir_upstream_error(status: int) -> None:
    client = make_client(
        lambda request: json_response({"error": "boom"}, status_code=status)
    )
    with pytest.raises(fhir.FhirUpstreamError) as exc_info:
        await client.read("Observation", "obs-1")
    assert exc_info.value.resource_type == "Observation"
    assert exc_info.value.status_code == status


async def test_non_json_body_raises_malformed_response() -> None:
    client = make_client(
        lambda request: httpx.Response(200, text="<html>not fhir</html>")
    )
    with pytest.raises(fhir.FhirMalformedResponse) as exc_info:
        await client.read("Patient", "abc")
    assert exc_info.value.resource_type == "Patient"


async def test_read_json_without_resource_type_is_malformed() -> None:
    client = make_client(lambda request: json_response({"unexpected": "shape"}))
    with pytest.raises(fhir.FhirMalformedResponse) as exc_info:
        await client.read("Patient", "abc")
    assert exc_info.value.resource_type == "Patient"


async def test_search_json_that_is_not_a_bundle_is_malformed() -> None:
    client = make_client(lambda request: json_response(patient("not-a-bundle")))
    with pytest.raises(fhir.FhirMalformedResponse) as exc_info:
        await client.search("AllergyIntolerance")
    assert exc_info.value.resource_type == "AllergyIntolerance"


async def test_search_errors_also_carry_resource_type() -> None:
    client = make_client(
        lambda request: json_response({"error": "denied"}, status_code=403)
    )
    with pytest.raises(fhir.FhirAuthError) as exc_info:
        await client.search("MedicationRequest", {"patient": "p1"})
    assert exc_info.value.resource_type == "MedicationRequest"
    assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# Criterion 4: configurable per-request timeout
# ---------------------------------------------------------------------------


async def test_transport_sleeping_past_timeout_raises_fhir_timeout() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.5)
        return json_response(patient("slow"))

    client = make_client(handler, timeout=0.05)
    with pytest.raises(fhir.FhirTimeout) as exc_info:
        await client.read("Patient", "slow")
    assert exc_info.value.resource_type == "Patient"


async def test_httpx_timeout_exception_maps_to_fhir_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    client = make_client(handler, timeout=1.0)
    with pytest.raises(fhir.FhirTimeout) as exc_info:
        await client.search("Encounter", {"patient": "p1"})
    assert exc_info.value.resource_type == "Encounter"


async def test_configured_timeout_is_applied_to_outgoing_requests() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["timeout"] = request.extensions.get("timeout")
        return json_response(patient("p1"))

    client = make_client(handler, timeout=2.5)
    await client.read("Patient", "p1")

    assert captured["timeout"] is not None
    assert captured["timeout"]["read"] == pytest.approx(2.5)


# ---------------------------------------------------------------------------
# Criterion 5: pagination with page cap
# ---------------------------------------------------------------------------


async def test_search_follows_next_links_and_combines_entries() -> None:
    seen: list[httpx.Request] = []
    client = make_client(three_page_handler(seen))

    result = await client.search("Patient")

    assert len(seen) == 3
    assert [r["id"] for r in result.entries] == ["p1", "p2", "p3", "p4", "p5"]
    assert result.truncated is False


async def test_page_cap_surfaces_truncation_instead_of_silently_stopping() -> None:
    seen: list[httpx.Request] = []
    client = make_client(three_page_handler(seen), max_pages=2)

    result = await client.search("Patient")

    assert len(seen) == 2, "must stop fetching at the page cap"
    assert [r["id"] for r in result.entries] == ["p1", "p2", "p3", "p4"]
    assert result.truncated is True


async def test_empty_bundle_returns_no_entries_and_not_truncated() -> None:
    client = make_client(lambda request: json_response(bundle([])))

    result = await client.search("Immunization", {"patient": "p1"})

    assert result.entries == []
    assert result.truncated is False


# ---------------------------------------------------------------------------
# T046 criterion 3: outbound FHIR requests forward the active correlation ID
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def active_correlation_id(value: str) -> Iterator[None]:
    """Set the request-scoped correlation ID contextvar for the duration of
    the ``with`` block, mirroring what ``CorrelationIdMiddleware`` does for a
    real in-flight request (T001) -- without needing a full ASGI app just to
    unit-test the FHIR client's forwarding behavior."""
    token = correlation._correlation_id.set(value)
    try:
        yield
    finally:
        correlation._correlation_id.reset(token)


async def test_read_forwards_the_active_correlation_id_header() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return json_response(patient("abc-123"))

    with active_correlation_id("corr-fhir-999"):
        client = make_client(handler, token="user-token-1")
        await client.read("Patient", "abc-123")

    assert len(seen) == 1
    assert seen[0].headers[correlation.CORRELATION_ID_HEADER.lower()] == "corr-fhir-999"


async def test_search_pagination_forwards_the_same_correlation_id_on_every_page() -> None:
    seen: list[httpx.Request] = []

    with active_correlation_id("corr-fhir-paging-777"):
        client = make_client(three_page_handler(seen))
        await client.search("Patient")

    assert len(seen) == 3
    assert all(
        r.headers[correlation.CORRELATION_ID_HEADER.lower()] == "corr-fhir-paging-777"
        for r in seen
    )


async def test_different_calls_forward_different_active_correlation_ids() -> None:
    """Adversarial: proves the header tracks the *active* contextvar per call
    rather than a value cached at client construction time."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return json_response(patient("abc-123"))

    client = make_client(handler, token="user-token-1")

    with active_correlation_id("corr-fhir-first"):
        await client.read("Patient", "abc-123")
    with active_correlation_id("corr-fhir-second"):
        await client.read("Patient", "abc-123")

    assert len(seen) == 2
    assert seen[0].headers[correlation.CORRELATION_ID_HEADER.lower()] == "corr-fhir-first"
    assert seen[1].headers[correlation.CORRELATION_ID_HEADER.lower()] == "corr-fhir-second"


async def test_no_correlation_id_header_sent_when_none_is_active() -> None:
    """Adversarial: outside any request context (the active contextvar is
    unset), the client must not invent a header out of nothing."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return json_response(patient("abc-123"))

    assert correlation.get_correlation_id() is None  # sanity: nothing leaked from another test
    client = make_client(handler, token="user-token-1")
    await client.read("Patient", "abc-123")

    assert len(seen) == 1
    assert correlation.CORRELATION_ID_HEADER.lower() not in seen[0].headers
