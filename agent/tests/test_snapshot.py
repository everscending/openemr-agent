"""Tests for get_patient_snapshot — parallel fan-out with graceful degradation (T005).

Criteria map:
  1. Six categories fetched concurrently (wall-clock ~ one delay, not six)
  2. Snapshot output contract: every record carries a ResourceRef; coverage
     lists all six categories on every call
  3. One category's failure degrades that category only (labs timeout,
     meds upstream error, demographics failure); no category ever missing
  4. Labs run under their own configurable timeout <= the overall deadline
  5. Zero records => verified_empty with a query receipt (never unavailable);
     the labs count bound is visible in the receipt
  6. Medication records state their source
  7. No request ever carries _revinclude (default fetchers over MockTransport)

The snapshot module is imported lazily inside tests so that collection
succeeds before the implementation exists (RED = ImportError inside tests).
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest

from copilot import contracts
from copilot.fhir import FhirTimeout, FhirUpstreamError

BASE_URL = "https://emr.example.test/apis/default/fhir"
PATIENT_ID = "pat-1"
TOKEN = "user-token"
AWARE = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)

EXPECTED_CATEGORIES = frozenset(
    {
        "demographics",
        "medications",
        "problems",
        "allergies",
        "labs",
        "last_encounter",
    }
)


def snapshot_mod() -> Any:
    from copilot.tools import snapshot

    return snapshot


# ---------------------------------------------------------------------------
# Record builders (called inside test bodies, never at collection time)
# ---------------------------------------------------------------------------


def make_ref(resource_type: str, resource_id: str) -> contracts.ResourceRef:
    return contracts.ResourceRef(
        resource_type=resource_type, resource_id=resource_id
    )


def patient_record() -> contracts.PatientRecord:
    return contracts.PatientRecord(
        ref=make_ref("Patient", PATIENT_ID), name="Ada Lovelace"
    )


def medication_record() -> contracts.MedicationRecord:
    return contracts.MedicationRecord(
        ref=make_ref("MedicationRequest", "med-1"),
        medication="Lisinopril 10mg",
        status="active",
        source="prescriptions",
    )


def condition_record() -> contracts.ConditionRecord:
    return contracts.ConditionRecord(
        ref=make_ref("Condition", "cond-1"), display="Hypertension"
    )


def allergy_record() -> contracts.AllergyRecord:
    return contracts.AllergyRecord(
        ref=make_ref("AllergyIntolerance", "alg-1"), display="Penicillin"
    )


def observation_record() -> contracts.ObservationRecord:
    return contracts.ObservationRecord(
        ref=make_ref("Observation", "obs-1"),
        code="718-7",
        display="Hemoglobin",
        value="13.2 g/dL",
        effective=AWARE,
    )


def encounter_record() -> contracts.EncounterRecord:
    return contracts.EncounterRecord(
        ref=make_ref("Encounter", "enc-1"), start=AWARE, reason="Annual physical"
    )


CATEGORY_RECORDS: dict[str, Any] = {
    "demographics": patient_record,
    "medications": medication_record,
    "problems": condition_record,
    "allergies": allergy_record,
    "labs": observation_record,
    "last_encounter": encounter_record,
}


# ---------------------------------------------------------------------------
# Fake fetcher helpers
# ---------------------------------------------------------------------------


def make_fetchers(
    mod: Any,
    *,
    delay: float = 0.0,
    overrides: dict[str, Any] | None = None,
    empty: frozenset[str] = frozenset(),
    starts: dict[str, float] | None = None,
) -> Any:
    """Build a full SnapshotFetchers set of fakes, with per-category overrides."""

    def fetcher_for(category: str) -> Any:
        async def fetcher(pid: str) -> Any:
            if starts is not None:
                starts[category] = time.monotonic()
            if delay:
                await asyncio.sleep(delay)
            records = () if category in empty else (CATEGORY_RECORDS[category](),)
            return mod.CategoryFetchResult(
                records=records,
                query_description=f"{category} query for patient {pid}",
                scope=f"all {category} records for patient {pid}",
            )

        return fetcher

    kwargs = {category: fetcher_for(category) for category in EXPECTED_CATEGORIES}
    if overrides:
        kwargs.update(overrides)
    return mod.SnapshotFetchers(**kwargs)


def raising_fetcher(exc: Exception) -> Any:
    async def fetcher(pid: str) -> Any:
        raise exc

    return fetcher


def coverage_by_category(result: Any) -> dict[str, Any]:
    return {entry.category: entry for entry in result.coverage}


# ---------------------------------------------------------------------------
# MockTransport FHIR server (drives the DEFAULT fetchers)
# ---------------------------------------------------------------------------


def json_response(payload: dict[str, Any], status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


def bundle(resources: list[dict[str, Any]]) -> dict[str, Any]:
    body: dict[str, Any] = {"resourceType": "Bundle", "type": "searchset"}
    if resources:
        body["entry"] = [{"resource": r} for r in resources]
    return body


def fhir_handler(
    seen: list[httpx.Request], *, empty_labs: bool = False
) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path.endswith(f"/Patient/{PATIENT_ID}"):
            return json_response(
                {
                    "resourceType": "Patient",
                    "id": PATIENT_ID,
                    "name": [{"text": "Ada Lovelace"}],
                    "birthDate": "1990-01-02",
                }
            )
        if path.endswith("/MedicationRequest"):
            return json_response(
                bundle(
                    [
                        {
                            "resourceType": "MedicationRequest",
                            "id": "med-1",
                            "status": "active",
                            "medicationCodeableConcept": {"text": "Lisinopril 10mg"},
                        }
                    ]
                )
            )
        if path.endswith("/Condition"):
            return json_response(
                bundle(
                    [
                        {
                            "resourceType": "Condition",
                            "id": "cond-1",
                            "code": {"text": "Hypertension"},
                        }
                    ]
                )
            )
        if path.endswith("/AllergyIntolerance"):
            return json_response(
                bundle(
                    [
                        {
                            "resourceType": "AllergyIntolerance",
                            "id": "alg-1",
                            "code": {"text": "Penicillin"},
                        }
                    ]
                )
            )
        if path.endswith("/Observation"):
            if empty_labs:
                return json_response(bundle([]))
            return json_response(
                bundle(
                    [
                        {
                            "resourceType": "Observation",
                            "id": "obs-1",
                            "code": {
                                "text": "Hemoglobin",
                                "coding": [{"code": "718-7", "display": "Hemoglobin"}],
                            },
                            "valueQuantity": {"value": 13.2, "unit": "g/dL"},
                            "effectiveDateTime": "2026-06-30T08:00:00+00:00",
                        }
                    ]
                )
            )
        if path.endswith("/Encounter"):
            return json_response(
                bundle(
                    [
                        {
                            "resourceType": "Encounter",
                            "id": "enc-1",
                            "period": {"start": "2026-06-15T09:30:00+00:00"},
                            "reasonCode": [{"text": "Annual physical"}],
                        }
                    ]
                )
            )
        return json_response({"error": "unexpected"}, status_code=404)

    return handler


async def run_default_snapshot(
    mod: Any, handler: Any, **kwargs: Any
) -> Any:
    return await mod.get_patient_snapshot(
        PATIENT_ID,
        TOKEN,
        base_url=BASE_URL,
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


# ==========================================================================
# Criterion 1 — six categories fetched concurrently
# ==========================================================================


async def test_six_categories_fetched_concurrently() -> None:
    mod = snapshot_mod()
    starts: dict[str, float] = {}
    fetchers = make_fetchers(mod, delay=0.15, starts=starts)

    began = time.monotonic()
    result = await mod.get_patient_snapshot(PATIENT_ID, TOKEN, fetchers=fetchers)
    elapsed = time.monotonic() - began

    assert isinstance(result, contracts.PatientSnapshotOutput)
    # Six 0.15s fetches serially = 0.9s; concurrently ~ one delay.
    assert elapsed < 0.45, f"snapshot took {elapsed:.3f}s — fetches were serial"
    # All six fetchers started before the first one could have finished.
    assert set(starts) == EXPECTED_CATEGORIES
    spread = max(starts.values()) - min(starts.values())
    assert spread < 0.15, f"fetch starts spread over {spread:.3f}s — not concurrent"


# ==========================================================================
# Criterion 2 — snapshot output contract, coverage always lists six
# ==========================================================================


async def test_happy_path_returns_snapshot_contract_with_refs() -> None:
    mod = snapshot_mod()
    result = await mod.get_patient_snapshot(
        PATIENT_ID, TOKEN, fetchers=make_fetchers(mod)
    )

    assert isinstance(result, contracts.PatientSnapshotOutput)
    assert isinstance(result.patient, contracts.PatientRecord)
    assert result.medications and result.conditions and result.allergies
    assert result.labs
    assert isinstance(result.last_encounter, contracts.EncounterRecord)

    all_records = [
        result.patient,
        *result.medications,
        *result.conditions,
        *result.allergies,
        *result.labs,
        result.last_encounter,
    ]
    for record in all_records:
        assert isinstance(record.ref, contracts.ResourceRef)


async def test_coverage_contains_exactly_the_six_categories() -> None:
    mod = snapshot_mod()
    result = await mod.get_patient_snapshot(
        PATIENT_ID, TOKEN, fetchers=make_fetchers(mod)
    )

    assert len(result.coverage) == 6
    assert {entry.category for entry in result.coverage} == EXPECTED_CATEGORIES
    for entry in result.coverage:
        assert entry.status == "ok"
        assert isinstance(entry, contracts.CoverageOk)
        assert entry.record_count >= 1


def test_snapshot_module_declares_the_six_categories() -> None:
    mod = snapshot_mod()
    assert set(mod.SNAPSHOT_CATEGORIES) == EXPECTED_CATEGORIES


# ==========================================================================
# Criterion 3 — one category's failure never fails the snapshot
# ==========================================================================


async def test_labs_timeout_degrades_only_labs() -> None:
    mod = snapshot_mod()
    fetchers = make_fetchers(
        mod,
        overrides={
            "labs": raising_fetcher(
                FhirTimeout(
                    "FHIR request for Observation timed out after 5.0s",
                    resource_type="Observation",
                )
            )
        },
    )

    result = await mod.get_patient_snapshot(PATIENT_ID, TOKEN, fetchers=fetchers)

    cov = coverage_by_category(result)
    assert set(cov) == EXPECTED_CATEGORIES, "a category went missing from coverage"
    assert cov["labs"].status == "unavailable"
    assert isinstance(cov["labs"], contracts.CoverageUnavailable)
    assert "timeout" in cov["labs"].reason.lower() or "timed out" in cov["labs"].reason.lower()
    assert result.labs == ()
    for category in EXPECTED_CATEGORIES - {"labs"}:
        assert cov[category].status == "ok", f"{category} should be ok"


async def test_meds_upstream_error_degrades_only_meds() -> None:
    mod = snapshot_mod()
    fetchers = make_fetchers(
        mod,
        overrides={
            "medications": raising_fetcher(
                FhirUpstreamError(
                    "FHIR upstream failure for MedicationRequest (500)",
                    resource_type="MedicationRequest",
                    status_code=500,
                )
            )
        },
    )

    result = await mod.get_patient_snapshot(PATIENT_ID, TOKEN, fetchers=fetchers)

    cov = coverage_by_category(result)
    assert set(cov) == EXPECTED_CATEGORIES, "a category went missing from coverage"
    assert cov["medications"].status == "unavailable"
    assert cov["medications"].reason
    assert result.medications == ()
    for category in EXPECTED_CATEGORIES - {"medications"}:
        assert cov[category].status == "ok", f"{category} should be ok"


async def test_demographics_failure_still_yields_full_coverage() -> None:
    mod = snapshot_mod()
    fetchers = make_fetchers(
        mod,
        overrides={
            "demographics": raising_fetcher(
                FhirUpstreamError(
                    "FHIR upstream failure for Patient (503)",
                    resource_type="Patient",
                    status_code=503,
                )
            )
        },
    )

    result = await mod.get_patient_snapshot(PATIENT_ID, TOKEN, fetchers=fetchers)

    cov = coverage_by_category(result)
    assert set(cov) == EXPECTED_CATEGORIES
    assert cov["demographics"].status == "unavailable"
    assert result.patient is None
    for category in EXPECTED_CATEGORIES - {"demographics"}:
        assert cov[category].status == "ok"


# ==========================================================================
# Criterion 4 — labs run under their own configurable timeout
# ==========================================================================


async def test_slow_labs_hits_labs_timeout_while_rest_completes() -> None:
    mod = snapshot_mod()

    async def slow_labs(pid: str) -> Any:
        await asyncio.sleep(0.5)
        return mod.CategoryFetchResult(
            records=(observation_record(),),
            query_description="labs",
            scope="labs",
        )

    fetchers = make_fetchers(mod, overrides={"labs": slow_labs})

    began = time.monotonic()
    result = await mod.get_patient_snapshot(
        PATIENT_ID, TOKEN, fetchers=fetchers, labs_timeout=0.1
    )
    elapsed = time.monotonic() - began

    assert elapsed < 0.4, "snapshot waited for the slow labs fetch"
    cov = coverage_by_category(result)
    assert cov["labs"].status == "unavailable"
    assert "timeout" in cov["labs"].reason.lower() or "timed out" in cov["labs"].reason.lower()
    assert result.labs == ()
    for category in EXPECTED_CATEGORIES - {"labs"}:
        assert cov[category].status == "ok"


async def test_labs_timeout_may_not_exceed_overall_deadline() -> None:
    mod = snapshot_mod()
    fetchers = make_fetchers(mod)
    with pytest.raises(ValueError):
        await mod.get_patient_snapshot(
            PATIENT_ID, TOKEN, fetchers=fetchers, labs_timeout=10.0, timeout=5.0
        )


# ==========================================================================
# Criterion 5 — verified_empty with a query receipt, never unavailable
# ==========================================================================


async def test_zero_allergies_yields_verified_empty_with_receipt() -> None:
    mod = snapshot_mod()
    fetchers = make_fetchers(mod, empty=frozenset({"allergies"}))

    result = await mod.get_patient_snapshot(PATIENT_ID, TOKEN, fetchers=fetchers)

    cov = coverage_by_category(result)
    entry = cov["allergies"]
    assert entry.status == "verified_empty"
    assert isinstance(entry, contracts.CoverageVerifiedEmpty)
    assert not isinstance(entry, contracts.CoverageUnavailable)
    assert entry.query_description
    assert entry.scope
    assert entry.timestamp.tzinfo is not None
    assert result.allergies == ()


async def test_empty_labs_receipt_shows_the_count_bound() -> None:
    mod = snapshot_mod()
    seen: list[httpx.Request] = []
    result = await run_default_snapshot(
        mod, fhir_handler(seen, empty_labs=True)
    )

    cov = coverage_by_category(result)
    entry = cov["labs"]
    assert entry.status == "verified_empty"
    assert "20" in entry.query_description, (
        "labs count bound must be visible in the query receipt"
    )


# ==========================================================================
# Criterion 6 — medication records state their source
# ==========================================================================


def test_medication_record_contract_carries_source() -> None:
    record = medication_record()
    assert record.source == "prescriptions"


async def test_snapshot_medications_each_state_a_source() -> None:
    mod = snapshot_mod()
    seen: list[httpx.Request] = []
    result = await run_default_snapshot(mod, fhir_handler(seen))

    assert result.medications
    for med in result.medications:
        assert isinstance(med.source, str) and med.source


# ==========================================================================
# Criterion 7 — no _revinclude on any request (default fetchers)
# ==========================================================================


async def test_default_fetchers_never_request_revinclude() -> None:
    mod = snapshot_mod()
    seen: list[httpx.Request] = []
    result = await run_default_snapshot(mod, fhir_handler(seen))

    # All six categories actually issued requests...
    assert len(seen) >= 6
    cov = coverage_by_category(result)
    assert set(cov) == EXPECTED_CATEGORIES
    assert all(entry.status == "ok" for entry in result.coverage)
    # ...and none of them asked for provenance rev-includes.
    for request in seen:
        assert "_revinclude" not in str(request.url), (
            f"forbidden _revinclude in {request.url}"
        )


# ==========================================================================
# Default-fetcher end-to-end sanity (criteria 2 + 6 over real FHIR shapes)
# ==========================================================================


async def test_default_fetchers_map_fhir_resources_to_records() -> None:
    mod = snapshot_mod()
    seen: list[httpx.Request] = []
    result = await run_default_snapshot(mod, fhir_handler(seen))

    assert result.patient is not None
    assert result.patient.name == "Ada Lovelace"
    assert result.patient.ref == make_ref("Patient", PATIENT_ID)
    assert [m.medication for m in result.medications] == ["Lisinopril 10mg"]
    assert [c.display for c in result.conditions] == ["Hypertension"]
    assert [a.display for a in result.allergies] == ["Penicillin"]
    assert [o.code for o in result.labs] == ["718-7"]
    assert result.last_encounter is not None
    assert result.last_encounter.ref == make_ref("Encounter", "enc-1")
    # Every request carried the user's token (passthrough, no agent creds).
    assert all(
        r.headers["authorization"] == f"Bearer {TOKEN}" for r in seen
    )
