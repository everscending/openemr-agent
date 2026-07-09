"""Tests for the five targeted retrieval tools (T006).

Criteria map:
  1. Each tool accepts its T002 input contract, returns its T002 output
     contract, and every record carries a ResourceRef of the right type.
  2. search_observations sends code / category / date-range (ge & le) /
     _sort=-date query params (asserted via captured requests on the mock
     transport) and returns records newest-first even for a shuffled Bundle
     (the tool sorts; it does not trust server order).
  3. get_encounters_since returns only encounters on/after the reference
     date (straddling mock Bundle), bounds results to a configurable cap
     with the cap-hit surfaced via `truncated` (never silent), and rejects
     a future reference date via input validation.
  4. get_medication_history returns active AND historical records, each
     carrying its status and a non-empty source indicator.
  5. Zero results => the output carries a query receipt (what was searched,
     scope, aware timestamp); receipt is None when records exist. Per tool.
  6. FHIR failures propagate as T003 typed errors — never swallowed.

The targeted-tools module is imported lazily inside tests so collection
succeeds before the implementation exists (RED = ImportError /
AttributeError / missing-validation failures inside tests).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from copilot import contracts
from copilot.fhir import FhirClient, FhirTimeout, FhirUpstreamError

BASE_URL = "https://emr.example.test/apis/default/fhir"
PATIENT_ID = "pat-1"
TOKEN = "user-token"

SINCE = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
RANGE_START = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
RANGE_END = datetime(2026, 6, 30, 23, 59, 59, tzinfo=timezone.utc)


def targeted_mod() -> Any:
    from copilot.tools import targeted

    return targeted


# ---------------------------------------------------------------------------
# Canned FHIR resources
# ---------------------------------------------------------------------------


def observation(obs_id: str, effective: str) -> dict[str, Any]:
    return {
        "resourceType": "Observation",
        "id": obs_id,
        "code": {
            "text": "Hemoglobin",
            "coding": [{"code": "718-7", "display": "Hemoglobin"}],
        },
        "valueQuantity": {"value": 13.2, "unit": "g/dL"},
        "effectiveDateTime": effective,
    }


def medication(med_id: str, status: str) -> dict[str, Any]:
    return {
        "resourceType": "MedicationRequest",
        "id": med_id,
        "status": status,
        "medicationCodeableConcept": {"text": f"Drug {med_id}"},
    }


def encounter(enc_id: str, start: str) -> dict[str, Any]:
    return {
        "resourceType": "Encounter",
        "id": enc_id,
        "period": {"start": start},
        "reasonCode": [{"text": "Follow-up"}],
    }


def document(doc_id: str) -> dict[str, Any]:
    return {
        "resourceType": "DocumentReference",
        "id": doc_id,
        "description": "Discharge summary",
        "date": "2026-06-20T10:00:00+00:00",
    }


def immunization(imm_id: str) -> dict[str, Any]:
    return {
        "resourceType": "Immunization",
        "id": imm_id,
        "vaccineCode": {"text": "Influenza, seasonal"},
        "occurrenceDateTime": "2026-05-01T00:00:00+00:00",
    }


# ---------------------------------------------------------------------------
# Mock transport helpers
# ---------------------------------------------------------------------------


def bundle(resources: list[dict[str, Any]]) -> dict[str, Any]:
    body: dict[str, Any] = {"resourceType": "Bundle", "type": "searchset"}
    if resources:
        body["entry"] = [{"resource": r} for r in resources]
    return body


def bundle_handler(
    resources: list[dict[str, Any]], seen: list[httpx.Request] | None = None
) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(200, json=bundle(resources))

    return handler


def status_handler(status_code: int) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": "boom"})

    return handler


def timeout_handler(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("simulated timeout", request=request)


async def run_tool(
    tool_name: str, tool_input: Any, handler: Any, **kwargs: Any
) -> Any:
    mod = targeted_mod()
    tool = getattr(mod, tool_name)
    async with FhirClient(
        BASE_URL, TOKEN, transport=httpx.MockTransport(handler)
    ) as client:
        return await tool(tool_input, client, **kwargs)


# ---------------------------------------------------------------------------
# Per-tool parametrization: (tool fn name, input builder, output model name,
# happy-path resource, expected ref resource type, expected URL path suffix)
# ---------------------------------------------------------------------------


def observations_input() -> Any:
    return contracts.SearchObservationsInput(patient_id=PATIENT_ID)


def medications_input() -> Any:
    return contracts.GetMedicationHistoryInput(patient_id=PATIENT_ID)


def encounters_input() -> Any:
    return contracts.GetEncountersSinceInput(patient_id=PATIENT_ID, since=SINCE)


def documents_input() -> Any:
    return contracts.SearchDocumentsInput(patient_id=PATIENT_ID)


def immunizations_input() -> Any:
    return contracts.GetImmunizationsInput(patient_id=PATIENT_ID)


TOOL_CASES = [
    pytest.param(
        "search_observations",
        observations_input,
        "SearchObservationsOutput",
        observation("obs-1", "2026-06-15T08:00:00+00:00"),
        "Observation",
        "/Observation",
        id="search_observations",
    ),
    pytest.param(
        "get_medication_history",
        medications_input,
        "GetMedicationHistoryOutput",
        medication("med-1", "active"),
        "MedicationRequest",
        "/MedicationRequest",
        id="get_medication_history",
    ),
    pytest.param(
        "get_encounters_since",
        encounters_input,
        "GetEncountersSinceOutput",
        encounter("enc-1", "2026-07-02T09:00:00+00:00"),
        "Encounter",
        "/Encounter",
        id="get_encounters_since",
    ),
    pytest.param(
        "search_documents",
        documents_input,
        "SearchDocumentsOutput",
        document("doc-1"),
        "DocumentReference",
        "/DocumentReference",
        id="search_documents",
    ),
    pytest.param(
        "get_immunizations",
        immunizations_input,
        "GetImmunizationsOutput",
        immunization("imm-1"),
        "Immunization",
        "/Immunization",
        id="get_immunizations",
    ),
]


# ==========================================================================
# Criterion 1 — input contract in, output contract out, ResourceRef on every
# record with the right resource type
# ==========================================================================


@pytest.mark.parametrize(
    ("tool_name", "input_builder", "output_name", "resource", "ref_type", "path"),
    TOOL_CASES,
)
async def test_tool_happy_path_returns_output_contract_with_typed_refs(
    tool_name: str,
    input_builder: Any,
    output_name: str,
    resource: dict[str, Any],
    ref_type: str,
    path: str,
) -> None:
    seen: list[httpx.Request] = []
    result = await run_tool(
        tool_name, input_builder(), bundle_handler([resource], seen)
    )

    assert isinstance(result, getattr(contracts, output_name))
    assert len(result.records) == 1
    record = result.records[0]
    assert isinstance(record.ref, contracts.ResourceRef)
    assert record.ref.resource_type == contracts.FhirResourceType(ref_type)
    assert record.ref.resource_id == resource["id"]

    # The tool queried the right resource endpoint, patient-scoped, with
    # the user's token passed through.
    assert len(seen) >= 1
    assert seen[0].url.path.endswith(path)
    assert seen[0].url.params["patient"] == PATIENT_ID
    assert seen[0].headers["authorization"] == f"Bearer {TOKEN}"


# ==========================================================================
# Criterion 2 — search_observations query params + newest-first ordering
# ==========================================================================


async def test_search_observations_sends_code_category_range_and_sort() -> None:
    seen: list[httpx.Request] = []
    tool_input = contracts.SearchObservationsInput(
        patient_id=PATIENT_ID,
        code="718-7",
        category="laboratory",
        start=RANGE_START,
        end=RANGE_END,
    )
    await run_tool(
        "search_observations",
        tool_input,
        bundle_handler([observation("obs-1", "2026-06-15T08:00:00+00:00")], seen),
    )

    assert len(seen) == 1
    params = seen[0].url.params
    assert params["patient"] == PATIENT_ID
    assert params["code"] == "718-7"
    assert params["category"] == "laboratory"
    assert params["_sort"] == "-date"
    dates = params.get_list("date")
    assert any(d.startswith("ge") and "2026-06-01" in d for d in dates), dates
    assert any(d.startswith("le") and "2026-06-30" in d for d in dates), dates


async def test_search_observations_omits_absent_filters() -> None:
    seen: list[httpx.Request] = []
    await run_tool(
        "search_observations",
        contracts.SearchObservationsInput(patient_id=PATIENT_ID),
        bundle_handler([observation("obs-1", "2026-06-15T08:00:00+00:00")], seen),
    )

    params = seen[0].url.params
    assert "code" not in params
    assert "category" not in params
    assert "date" not in params
    assert params["_sort"] == "-date"


async def test_search_observations_sorts_shuffled_bundle_newest_first() -> None:
    shuffled = [
        observation("obs-mid", "2026-06-15T08:00:00+00:00"),
        observation("obs-new", "2026-06-28T08:00:00+00:00"),
        observation("obs-old", "2026-06-02T08:00:00+00:00"),
    ]
    result = await run_tool(
        "search_observations",
        contracts.SearchObservationsInput(patient_id=PATIENT_ID),
        bundle_handler(shuffled),
    )

    ids = [r.ref.resource_id for r in result.records]
    assert ids == ["obs-new", "obs-mid", "obs-old"]
    effectives = [r.effective for r in result.records]
    assert effectives == sorted(effectives, reverse=True)


# ==========================================================================
# Criterion 3 — get_encounters_since: on/after filter, bounded batches with
# surfaced cap-hit, future reference date rejected
# ==========================================================================


async def test_encounters_since_returns_only_on_or_after_reference() -> None:
    straddling = [
        encounter("enc-before", "2026-05-20T09:00:00+00:00"),
        encounter("enc-on", "2026-06-01T00:00:00+00:00"),
        encounter("enc-after", "2026-06-15T09:00:00+00:00"),
    ]
    seen: list[httpx.Request] = []
    result = await run_tool(
        "get_encounters_since",
        encounters_input(),
        bundle_handler(straddling, seen),
    )

    ids = {r.ref.resource_id for r in result.records}
    assert ids == {"enc-on", "enc-after"}, (
        "must include the boundary date and exclude strictly-before"
    )
    # The server-side filter was requested too (date=ge{since}).
    params = seen[0].url.params
    dates = params.get_list("date")
    assert any(d.startswith("ge") and "2026-06-01" in d for d in dates), dates
    assert params["_sort"] == "-date"


async def test_encounters_since_bounds_batch_and_surfaces_cap_hit() -> None:
    many = [
        encounter("enc-1", "2026-06-05T09:00:00+00:00"),
        encounter("enc-4", "2026-06-20T09:00:00+00:00"),
        encounter("enc-2", "2026-06-10T09:00:00+00:00"),
        encounter("enc-3", "2026-06-15T09:00:00+00:00"),
    ]
    seen: list[httpx.Request] = []
    result = await run_tool(
        "get_encounters_since",
        encounters_input(),
        bundle_handler(many, seen),
        cap=2,
    )

    # Bounded to the cap, newest-first, and the cap hit is surfaced.
    assert len(result.records) == 2
    assert [r.ref.resource_id for r in result.records] == ["enc-4", "enc-3"]
    assert result.truncated is True
    # The cap was also requested server-side.
    assert seen[0].url.params["_count"] == "2"


async def test_encounters_since_under_cap_is_not_truncated() -> None:
    result = await run_tool(
        "get_encounters_since",
        encounters_input(),
        bundle_handler([encounter("enc-1", "2026-06-15T09:00:00+00:00")]),
        cap=2,
    )

    assert len(result.records) == 1
    assert result.truncated is False


def test_encounters_since_input_rejects_future_reference_date() -> None:
    future = datetime.now(timezone.utc) + timedelta(days=1)
    with pytest.raises(ValidationError):
        contracts.GetEncountersSinceInput(patient_id=PATIENT_ID, since=future)


def test_encounters_since_input_accepts_past_reference_date() -> None:
    tool_input = contracts.GetEncountersSinceInput(
        patient_id=PATIENT_ID, since=SINCE
    )
    assert tool_input.since == SINCE


# ==========================================================================
# Criterion 4 — get_medication_history: active AND historical, status +
# source on every record
# ==========================================================================


async def test_medication_history_returns_all_statuses_with_source() -> None:
    mixed = [
        medication("med-active", "active"),
        medication("med-stopped", "stopped"),
        medication("med-completed", "completed"),
    ]
    seen: list[httpx.Request] = []
    result = await run_tool(
        "get_medication_history",
        medications_input(),
        bundle_handler(mixed, seen),
    )

    assert len(result.records) == 3
    by_id = {r.ref.resource_id: r for r in result.records}
    assert by_id["med-active"].status == "active"
    assert by_id["med-stopped"].status == "stopped"
    assert by_id["med-completed"].status == "completed"
    for record in result.records:
        assert isinstance(record.source, str) and record.source

    # History means no status filter on the search.
    assert "status" not in seen[0].url.params


# ==========================================================================
# Criterion 5 — zero results => query receipt (what was searched, scope,
# aware timestamp); receipt absent when records exist
# ==========================================================================


@pytest.mark.parametrize(
    ("tool_name", "input_builder", "output_name", "resource", "ref_type", "path"),
    TOOL_CASES,
)
async def test_zero_results_carry_a_query_receipt(
    tool_name: str,
    input_builder: Any,
    output_name: str,
    resource: dict[str, Any],
    ref_type: str,
    path: str,
) -> None:
    result = await run_tool(tool_name, input_builder(), bundle_handler([]))

    assert result.records == ()
    receipt = result.receipt
    assert receipt is not None, f"{tool_name} returned a bare empty result"
    assert isinstance(receipt, contracts.QueryReceipt)
    assert ref_type in receipt.query_description, (
        "receipt must state which resource type was searched"
    )
    assert receipt.scope
    assert receipt.timestamp.tzinfo is not None


@pytest.mark.parametrize(
    ("tool_name", "input_builder", "output_name", "resource", "ref_type", "path"),
    TOOL_CASES,
)
async def test_nonempty_results_carry_no_receipt(
    tool_name: str,
    input_builder: Any,
    output_name: str,
    resource: dict[str, Any],
    ref_type: str,
    path: str,
) -> None:
    result = await run_tool(tool_name, input_builder(), bundle_handler([resource]))

    assert result.records
    assert result.receipt is None


async def test_empty_observations_receipt_states_the_filters() -> None:
    tool_input = contracts.SearchObservationsInput(
        patient_id=PATIENT_ID,
        code="718-7",
        category="laboratory",
        start=RANGE_START,
        end=RANGE_END,
    )
    result = await run_tool("search_observations", tool_input, bundle_handler([]))

    receipt = result.receipt
    assert receipt is not None
    assert "718-7" in receipt.query_description
    assert "laboratory" in receipt.query_description
    assert "2026-06-01" in receipt.query_description
    assert PATIENT_ID in receipt.scope


async def test_empty_encounters_receipt_states_the_reference_date() -> None:
    result = await run_tool(
        "get_encounters_since", encounters_input(), bundle_handler([])
    )

    receipt = result.receipt
    assert receipt is not None
    assert "2026-06-01" in receipt.query_description
    assert result.truncated is False


# ==========================================================================
# Criterion 6 — FHIR failures propagate as T003 typed errors
# ==========================================================================


@pytest.mark.parametrize(
    ("tool_name", "input_builder", "output_name", "resource", "ref_type", "path"),
    TOOL_CASES,
)
async def test_upstream_500_propagates_typed_error(
    tool_name: str,
    input_builder: Any,
    output_name: str,
    resource: dict[str, Any],
    ref_type: str,
    path: str,
) -> None:
    with pytest.raises(FhirUpstreamError):
        await run_tool(tool_name, input_builder(), status_handler(500))


@pytest.mark.parametrize(
    ("tool_name", "input_builder"),
    [
        pytest.param(
            "search_observations", observations_input, id="search_observations"
        ),
        pytest.param(
            "get_encounters_since", encounters_input, id="get_encounters_since"
        ),
    ],
)
async def test_timeout_propagates_typed_error(
    tool_name: str, input_builder: Any
) -> None:
    with pytest.raises(FhirTimeout):
        await run_tool(tool_name, input_builder(), timeout_handler)
