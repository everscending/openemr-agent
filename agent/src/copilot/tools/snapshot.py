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
from datetime import datetime, timezone
from typing import Awaitable, Callable

import httpx

from copilot.contracts.coverage import (
    CategoryCoverage,
    CoverageOk,
    CoverageUnavailable,
    CoverageVerifiedEmpty,
)
from copilot.contracts.tools import (
    SNAPSHOT_CATEGORIES,
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
from copilot.tools.reconciliation import reconcile_medications
from copilot.tools.mapping import (
    MEDICATION_SOURCE_PRESCRIPTIONS,
    allergy_record_from,
    condition_record_from,
    encounter_record_from,
    extract_records,
    medication_record_from,
    observation_record_from,
    patient_record_from,
)

DEFAULT_SNAPSHOT_TIMEOUT_SECONDS = 30.0
DEFAULT_LABS_TIMEOUT_SECONDS = 10.0
LABS_COUNT_BOUND = 20

# SNAPSHOT_CATEGORIES (the six categories, in reporting order) now lives in the
# contracts layer as the single source of truth and is imported above; it stays
# importable from this module for existing callers. Coverage output carries
# exactly one entry per category on every call.


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

    medication_records = tuple(
        r for r in by_category["medications"][0] if isinstance(r, MedicationRecord)
    )
    # Reconcile across sources so source conflicts surface and resolved meds
    # are split off (T007). None only when the category itself failed.
    medications_available = by_category["medications"][1].status != "unavailable"
    medication_reconciliation = (
        reconcile_medications(medication_records) if medications_available else None
    )

    return PatientSnapshotOutput(
        patient=(
            demographics_records[0]
            if demographics_records
            and isinstance(demographics_records[0], PatientRecord)
            else None
        ),
        medications=medication_records,
        medication_reconciliation=medication_reconciliation,
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
        record = patient_record_from(resource)
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
        records = extract_records(
            result.entries,
            lambda resource: medication_record_from(
                resource, source=MEDICATION_SOURCE_PRESCRIPTIONS
            ),
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
        records = extract_records(result.entries, condition_record_from)
        return CategoryFetchResult(
            records=records,
            query_description=f"Condition?patient={patient_id}",
            scope=f"all problem-list conditions for patient {patient_id}",
        )

    return fetch


def _allergies_fetcher(client: FhirClient) -> CategoryFetcher:
    async def fetch(patient_id: str) -> CategoryFetchResult:
        result = await client.search("AllergyIntolerance", {"patient": patient_id})
        records = extract_records(result.entries, allergy_record_from)
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
        records = extract_records(result.entries, observation_record_from)[
            :LABS_COUNT_BOUND
        ]
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
            record = encounter_record_from(entry)
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
