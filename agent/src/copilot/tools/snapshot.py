"""``get_patient_snapshot`` — parallel fan-out with graceful degradation (T005).

Fetches six patient-data categories concurrently in one agent step
(USER.md UC-1, ARCHITECTURE.md section 6): demographics, active
medications, active problems, allergies, recent labs, and the last
encounter. The governing invariant (ARCHITECTURE.md section 7) is that
the physician always knows what the agent did NOT check — every call
returns a coverage entry for all six categories, and a failed category
is reported ``unavailable`` with its reason, never silently omitted.

Labs are the slowest resource (AUDIT.md: no patient index on
``procedure_result``), so they run under their own, shorter timeout and
degrade independently of the rest of the snapshot.

Pure data tool: no LLM involvement. Medication conflict reconciliation
is out of scope (T007) — each medication record only states its source.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Awaitable, Callable

import httpx

from copilot.contracts.coverage import (
    CategoryCoverage,
    CoverageOk,
    CoverageUnavailable,
    CoverageVerifiedEmpty,
)
from copilot.contracts.refs import FhirResourceType, ResourceRef
from copilot.contracts.tools import (
    AllergyRecord,
    ConditionRecord,
    EncounterRecord,
    MedicationRecord,
    ObservationRecord,
    OutputRecord,
    PatientRecord,
    PatientSnapshotOutput,
)
from copilot.fhir import FhirClient

DEFAULT_SNAPSHOT_TIMEOUT_SECONDS = 30.0
DEFAULT_LABS_TIMEOUT_SECONDS = 10.0
LABS_COUNT_BOUND = 20
MEDICATION_SOURCE_PRESCRIPTIONS = "prescriptions"

# The six snapshot categories, in reporting order. Coverage output carries
# exactly one entry per category on every call.
SNAPSHOT_CATEGORIES: tuple[str, ...] = (
    "demographics",
    "medications",
    "problems",
    "allergies",
    "labs",
    "last_encounter",
)


@dataclass(frozen=True, slots=True)
class CategoryFetchResult:
    """One category's successful fetch: records plus its query receipt.

    The receipt (what was searched, over what scope) backs the
    ``verified_empty`` coverage entry when zero records come back —
    "no allergies recorded" must be distinguishable from "not checked".
    """

    records: tuple[OutputRecord, ...]
    query_description: str
    scope: str


CategoryFetcher = Callable[[str], Awaitable[CategoryFetchResult]]


@dataclass(frozen=True, slots=True)
class SnapshotFetchers:
    """The six injectable category fetchers backing one snapshot call."""

    demographics: CategoryFetcher
    medications: CategoryFetcher
    problems: CategoryFetcher
    allergies: CategoryFetcher
    labs: CategoryFetcher
    last_encounter: CategoryFetcher


async def get_patient_snapshot(
    patient_id: str,
    token: str,
    *,
    base_url: str = "",
    fetchers: SnapshotFetchers | None = None,
    labs_timeout: float = DEFAULT_LABS_TIMEOUT_SECONDS,
    timeout: float = DEFAULT_SNAPSHOT_TIMEOUT_SECONDS,
    transport: httpx.AsyncBaseTransport | None = None,
) -> PatientSnapshotOutput:
    """Fetch the six snapshot categories concurrently; degrade per category.

    When ``fetchers`` is not injected, default fetchers are built over a
    :class:`FhirClient` bound to ``base_url`` and the requesting user's
    ``token`` (passthrough — the agent holds no standing credentials).
    ``labs_timeout`` bounds only the labs fetch and must not exceed the
    overall snapshot ``timeout``.
    """
    if labs_timeout > timeout:
        raise ValueError(
            "labs_timeout must be less than or equal to the overall "
            "snapshot timeout"
        )
    if fetchers is not None:
        return await _run_snapshot(
            patient_id, fetchers, labs_timeout=labs_timeout, timeout=timeout
        )
    if not base_url:
        raise ValueError("base_url is required when no fetchers are injected")
    async with FhirClient(
        base_url, token, timeout=timeout, transport=transport
    ) as client:
        return await _run_snapshot(
            patient_id,
            build_default_fetchers(client),
            labs_timeout=labs_timeout,
            timeout=timeout,
        )


async def _run_snapshot(
    patient_id: str,
    fetchers: SnapshotFetchers,
    *,
    labs_timeout: float,
    timeout: float,
) -> PatientSnapshotOutput:
    outcomes = await asyncio.gather(
        *(
            _run_category(
                category,
                getattr(fetchers, category),
                patient_id,
                timeout=labs_timeout if category == "labs" else timeout,
            )
            for category in SNAPSHOT_CATEGORIES
        )
    )
    by_category = dict(zip(SNAPSHOT_CATEGORIES, outcomes, strict=True))

    demographics_records = by_category["demographics"][0]
    encounter_records = by_category["last_encounter"][0]

    return PatientSnapshotOutput(
        patient=(
            demographics_records[0]
            if demographics_records
            and isinstance(demographics_records[0], PatientRecord)
            else None
        ),
        medications=tuple(
            r for r in by_category["medications"][0] if isinstance(r, MedicationRecord)
        ),
        conditions=tuple(
            r for r in by_category["problems"][0] if isinstance(r, ConditionRecord)
        ),
        allergies=tuple(
            r for r in by_category["allergies"][0] if isinstance(r, AllergyRecord)
        ),
        labs=tuple(
            r for r in by_category["labs"][0] if isinstance(r, ObservationRecord)
        ),
        last_encounter=(
            encounter_records[0]
            if encounter_records and isinstance(encounter_records[0], EncounterRecord)
            else None
        ),
        coverage=tuple(by_category[category][1] for category in SNAPSHOT_CATEGORIES),
    )


async def _run_category(
    category: str,
    fetcher: CategoryFetcher,
    patient_id: str,
    *,
    timeout: float,
) -> tuple[tuple[OutputRecord, ...], CategoryCoverage]:
    """Run one category fetch; never raise — degrade into coverage instead."""
    try:
        async with asyncio.timeout(timeout):
            result = await fetcher(patient_id)
    except TimeoutError:
        return (), CoverageUnavailable(
            category=category,
            reason=f"timeout: category fetch exceeded {timeout}s",
        )
    except Exception as exc:  # graceful degradation: category-local failure
        return (), CoverageUnavailable(
            category=category,
            reason=f"{type(exc).__name__}: {exc}",
        )
    if not result.records:
        return (), CoverageVerifiedEmpty(
            category=category,
            query_description=result.query_description,
            scope=result.scope,
            timestamp=datetime.now(timezone.utc),
        )
    return result.records, CoverageOk(
        category=category, record_count=len(result.records)
    )


# ---------------------------------------------------------------------------
# Default fetchers over a FhirClient
# ---------------------------------------------------------------------------
#
# ARCHITECTURE.md section 6c: snapshot reads never request
# `_revinclude=provenance` — only plain reads/searches below.


def build_default_fetchers(client: FhirClient) -> SnapshotFetchers:
    """Build the six default category fetchers over ``client``."""
    return SnapshotFetchers(
        demographics=_demographics_fetcher(client),
        medications=_medications_fetcher(client),
        problems=_problems_fetcher(client),
        allergies=_allergies_fetcher(client),
        labs=_labs_fetcher(client),
        last_encounter=_last_encounter_fetcher(client),
    )


def _demographics_fetcher(client: FhirClient) -> CategoryFetcher:
    async def fetch(patient_id: str) -> CategoryFetchResult:
        resource = await client.read("Patient", patient_id)
        record = _patient_record(resource)
        return CategoryFetchResult(
            records=(record,) if record is not None else (),
            query_description=f"Patient/{patient_id}",
            scope=f"demographics for patient {patient_id}",
        )

    return fetch


def _medications_fetcher(client: FhirClient) -> CategoryFetcher:
    async def fetch(patient_id: str) -> CategoryFetchResult:
        result = await client.search(
            "MedicationRequest", {"patient": patient_id, "status": "active"}
        )
        records = tuple(
            record
            for record in (
                _medication_record(entry, source=MEDICATION_SOURCE_PRESCRIPTIONS)
                for entry in result.entries
            )
            if record is not None
        )
        return CategoryFetchResult(
            records=records,
            query_description=(
                f"MedicationRequest?patient={patient_id}&status=active"
            ),
            scope=(
                f"active medication requests ({MEDICATION_SOURCE_PRESCRIPTIONS}) "
                f"for patient {patient_id}"
            ),
        )

    return fetch


def _problems_fetcher(client: FhirClient) -> CategoryFetcher:
    async def fetch(patient_id: str) -> CategoryFetchResult:
        result = await client.search("Condition", {"patient": patient_id})
        records = tuple(
            record
            for record in (_condition_record(entry) for entry in result.entries)
            if record is not None
        )
        return CategoryFetchResult(
            records=records,
            query_description=f"Condition?patient={patient_id}",
            scope=f"all problem-list conditions for patient {patient_id}",
        )

    return fetch


def _allergies_fetcher(client: FhirClient) -> CategoryFetcher:
    async def fetch(patient_id: str) -> CategoryFetchResult:
        result = await client.search("AllergyIntolerance", {"patient": patient_id})
        records = tuple(
            record
            for record in (_allergy_record(entry) for entry in result.entries)
            if record is not None
        )
        return CategoryFetchResult(
            records=records,
            query_description=f"AllergyIntolerance?patient={patient_id}",
            scope=f"all allergy and intolerance records for patient {patient_id}",
        )

    return fetch


def _labs_fetcher(client: FhirClient) -> CategoryFetcher:
    async def fetch(patient_id: str) -> CategoryFetchResult:
        result = await client.search(
            "Observation",
            {
                "patient": patient_id,
                "category": "laboratory",
                "_sort": "-date",
                "_count": str(LABS_COUNT_BOUND),
            },
        )
        records = tuple(
            record
            for record in (_observation_record(entry) for entry in result.entries)
            if record is not None
        )[:LABS_COUNT_BOUND]
        return CategoryFetchResult(
            records=records,
            query_description=(
                f"Observation?patient={patient_id}&category=laboratory"
                f"&_sort=-date&_count={LABS_COUNT_BOUND}"
            ),
            scope=(
                f"most recent {LABS_COUNT_BOUND} laboratory observations "
                f"for patient {patient_id}"
            ),
        )

    return fetch


def _last_encounter_fetcher(client: FhirClient) -> CategoryFetcher:
    async def fetch(patient_id: str) -> CategoryFetchResult:
        result = await client.search(
            "Encounter", {"patient": patient_id, "_sort": "-date", "_count": "1"}
        )
        records: tuple[OutputRecord, ...] = ()
        for entry in result.entries:
            record = _encounter_record(entry)
            if record is not None:
                records = (record,)
                break
        return CategoryFetchResult(
            records=records,
            query_description=(
                f"Encounter?patient={patient_id}&_sort=-date&_count=1"
            ),
            scope=f"most recent encounter for patient {patient_id}",
        )

    return fetch


# ---------------------------------------------------------------------------
# FHIR resource -> record mapping
# ---------------------------------------------------------------------------


def _resource_id(resource: dict[str, Any]) -> str | None:
    resource_id = resource.get("id")
    if isinstance(resource_id, str) and resource_id:
        return resource_id
    return None


def _codeable_text(concept: Any) -> str | None:
    """Best-effort display text from a FHIR CodeableConcept."""
    if not isinstance(concept, dict):
        return None
    text = concept.get("text")
    if isinstance(text, str) and text:
        return text
    codings = concept.get("coding")
    if isinstance(codings, list):
        for coding in codings:
            if isinstance(coding, dict):
                display = coding.get("display")
                if isinstance(display, str) and display:
                    return display
    return None


def _codeable_code(concept: Any) -> str | None:
    if not isinstance(concept, dict):
        return None
    codings = concept.get("coding")
    if isinstance(codings, list):
        for coding in codings:
            if isinstance(coding, dict):
                code = coding.get("code")
                if isinstance(code, str) and code:
                    return code
    return None


def _aware_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _patient_record(resource: dict[str, Any]) -> PatientRecord | None:
    resource_id = _resource_id(resource)
    if resource_id is None:
        return None
    name = _human_name(resource.get("name")) or "Unknown"
    birth_raw = resource.get("birthDate")
    birth_date: date | None = None
    if isinstance(birth_raw, str) and birth_raw:
        try:
            birth_date = date.fromisoformat(birth_raw)
        except ValueError:
            birth_date = None
    return PatientRecord(
        ref=ResourceRef(
            resource_type=FhirResourceType.PATIENT, resource_id=resource_id
        ),
        name=name,
        birth_date=birth_date,
    )


def _human_name(names: Any) -> str | None:
    if not isinstance(names, list):
        return None
    for name in names:
        if not isinstance(name, dict):
            continue
        text = name.get("text")
        if isinstance(text, str) and text:
            return text
        given = name.get("given")
        given_parts = (
            [part for part in given if isinstance(part, str) and part]
            if isinstance(given, list)
            else []
        )
        family = name.get("family")
        parts = given_parts + (
            [family] if isinstance(family, str) and family else []
        )
        if parts:
            return " ".join(parts)
    return None


def _medication_record(
    resource: dict[str, Any], *, source: str
) -> MedicationRecord | None:
    resource_id = _resource_id(resource)
    if resource_id is None:
        return None
    medication = _codeable_text(resource.get("medicationCodeableConcept"))
    if medication is None:
        return None
    status = resource.get("status")
    return MedicationRecord(
        ref=ResourceRef(
            resource_type=FhirResourceType.MEDICATION_REQUEST,
            resource_id=resource_id,
        ),
        medication=medication,
        status=status if isinstance(status, str) and status else None,
        source=source,
    )


def _condition_record(resource: dict[str, Any]) -> ConditionRecord | None:
    resource_id = _resource_id(resource)
    if resource_id is None:
        return None
    display = _codeable_text(resource.get("code"))
    if display is None:
        return None
    return ConditionRecord(
        ref=ResourceRef(
            resource_type=FhirResourceType.CONDITION, resource_id=resource_id
        ),
        display=display,
    )


def _allergy_record(resource: dict[str, Any]) -> AllergyRecord | None:
    resource_id = _resource_id(resource)
    if resource_id is None:
        return None
    display = _codeable_text(resource.get("code"))
    if display is None:
        return None
    return AllergyRecord(
        ref=ResourceRef(
            resource_type=FhirResourceType.ALLERGY_INTOLERANCE,
            resource_id=resource_id,
        ),
        display=display,
    )


def _observation_record(resource: dict[str, Any]) -> ObservationRecord | None:
    resource_id = _resource_id(resource)
    if resource_id is None:
        return None
    code = _codeable_code(resource.get("code")) or _codeable_text(
        resource.get("code")
    )
    display = _codeable_text(resource.get("code"))
    if code is None or display is None:
        return None
    return ObservationRecord(
        ref=ResourceRef(
            resource_type=FhirResourceType.OBSERVATION, resource_id=resource_id
        ),
        code=code,
        display=display,
        value=_observation_value(resource),
        effective=_aware_datetime(resource.get("effectiveDateTime")),
    )


def _observation_value(resource: dict[str, Any]) -> str | None:
    quantity = resource.get("valueQuantity")
    if isinstance(quantity, dict):
        value = quantity.get("value")
        unit = quantity.get("unit")
        if isinstance(value, (int, float)):
            if isinstance(unit, str) and unit:
                return f"{value} {unit}"
            return str(value)
    value_string = resource.get("valueString")
    if isinstance(value_string, str) and value_string:
        return value_string
    return None


def _encounter_record(resource: dict[str, Any]) -> EncounterRecord | None:
    resource_id = _resource_id(resource)
    if resource_id is None:
        return None
    period = resource.get("period")
    start = _aware_datetime(period.get("start")) if isinstance(period, dict) else None
    if start is None:
        return None
    reason: str | None = None
    reason_codes = resource.get("reasonCode")
    if isinstance(reason_codes, list):
        for concept in reason_codes:
            reason = _codeable_text(concept)
            if reason is not None:
                break
    return EncounterRecord(
        ref=ResourceRef(
            resource_type=FhirResourceType.ENCOUNTER, resource_id=resource_id
        ),
        start=start,
        reason=reason,
    )
