"""Targeted retrieval tools (T006).

Five patient-scoped, read-only data tools beyond the snapshot
(USER.md UC-2/UC-3/UC-4): ``search_observations``,
``get_medication_history``, ``get_encounters_since``,
``search_documents``, ``get_immunizations``. Each takes its T002 input
contract plus a :class:`~copilot.fhir.FhirClient` and returns its T002
output contract, every record carrying a ``ResourceRef``.

Two invariants govern these tools:

* UC-4 query receipts: a zero-result answer is never a bare "no" — the
  output carries a :class:`~copilot.contracts.QueryReceipt` stating the
  exact query, its scope, and when it ran.
* No swallowing: FHIR failures propagate as the T003 typed errors.
  Snapshot-style graceful degradation is T005's job, not these tools'.

Pure data tools: no LLM involvement, no salience ranking or diffing
(agent-loop synthesis, T010), and ``search_documents`` returns document
metadata refs only — never content.
"""

from __future__ import annotations

from datetime import datetime, timezone

from copilot.contracts.tools import (
    EncounterRecord,
    GetEncountersSinceInput,
    GetEncountersSinceOutput,
    GetImmunizationsInput,
    GetImmunizationsOutput,
    GetMedicationHistoryInput,
    GetMedicationHistoryOutput,
    ObservationRecord,
    QueryReceipt,
    SearchDocumentsInput,
    SearchDocumentsOutput,
    SearchObservationsInput,
    SearchObservationsOutput,
)
from copilot.fhir import FhirClient
from copilot.tools.mapping import (
    MEDICATION_SOURCE_PRESCRIPTIONS,
    document_record_from,
    encounter_record_from,
    extract_records,
    immunization_record_from,
    medication_record_from,
    observation_record_from,
)

DEFAULT_ENCOUNTERS_CAP = 50

_EPOCH = datetime.min.replace(tzinfo=timezone.utc)

Params = dict[str, str | list[str]]


def _receipt(resource_type: str, params: Params, scope: str) -> QueryReceipt:
    """Build the UC-4 query receipt for a search that returned nothing."""
    parts: list[str] = []
    for key, value in params.items():
        values = value if isinstance(value, list) else [value]
        parts.extend(f"{key}={item}" for item in values)
    return QueryReceipt(
        query_description=f"{resource_type}?" + "&".join(parts),
        scope=scope,
        timestamp=datetime.now(timezone.utc),
    )


def _date_range_params(
    params: Params, start: datetime | None, end: datetime | None
) -> None:
    """Add inclusive FHIR date-range params (``ge``/``le``) when present."""
    dates: list[str] = []
    if start is not None:
        dates.append(f"ge{start.isoformat()}")
    if end is not None:
        dates.append(f"le{end.isoformat()}")
    if dates:
        params["date"] = dates


async def search_observations(
    tool_input: SearchObservationsInput, client: FhirClient
) -> SearchObservationsOutput:
    """Search observations by code/category/date range, newest-first.

    The server is asked to sort (``_sort=-date``) but the result is
    re-sorted locally so ordering never depends on upstream behaviour.
    """
    params: Params = {"patient": tool_input.patient_id}
    if tool_input.code is not None:
        params["code"] = tool_input.code
    if tool_input.category is not None:
        params["category"] = tool_input.category
    _date_range_params(params, tool_input.start, tool_input.end)
    params["_sort"] = "-date"

    result = await client.search("Observation", params)
    records = tuple(
        sorted(
            extract_records(result.entries, observation_record_from),
            key=_observation_sort_key,
            reverse=True,
        )
    )
    if records:
        return SearchObservationsOutput(records=records)
    return SearchObservationsOutput(
        records=(),
        receipt=_receipt(
            "Observation",
            params,
            f"observations matching the requested filters for patient "
            f"{tool_input.patient_id}",
        ),
    )


def _observation_sort_key(record: ObservationRecord) -> datetime:
    return record.effective if record.effective is not None else _EPOCH


async def get_medication_history(
    tool_input: GetMedicationHistoryInput, client: FhirClient
) -> GetMedicationHistoryOutput:
    """Fetch active AND historical medication records with status + source.

    No status filter: history means every status (active, stopped,
    completed, ...) comes back, each record stating its own status and
    which medication list it came from.
    """
    params: Params = {"patient": tool_input.patient_id}
    result = await client.search("MedicationRequest", params)
    records = extract_records(
        result.entries,
        lambda resource: medication_record_from(
            resource, source=MEDICATION_SOURCE_PRESCRIPTIONS
        ),
    )
    if records:
        return GetMedicationHistoryOutput(records=records)
    return GetMedicationHistoryOutput(
        records=(),
        receipt=_receipt(
            "MedicationRequest",
            params,
            f"medication history (all statuses, "
            f"{MEDICATION_SOURCE_PRESCRIPTIONS}) for patient "
            f"{tool_input.patient_id}",
        ),
    )


async def get_encounters_since(
    tool_input: GetEncountersSinceInput,
    client: FhirClient,
    *,
    cap: int = DEFAULT_ENCOUNTERS_CAP,
) -> GetEncountersSinceOutput:
    """Fetch encounters on/after the reference date in a bounded batch.

    Results are newest-first and bounded to ``cap``; hitting the cap is
    surfaced via ``truncated``, never silent (ARCHITECTURE.md section 6d).
    The on/after filter is requested server-side (``date=ge...``) and
    enforced locally so a permissive upstream cannot leak earlier
    encounters.
    """
    if cap < 1:
        raise ValueError("cap must be at least 1")
    params: Params = {
        "patient": tool_input.patient_id,
        "date": f"ge{tool_input.since.isoformat()}",
        "_sort": "-date",
        "_count": str(cap),
    }
    result = await client.search("Encounter", params)
    eligible = tuple(
        sorted(
            (
                record
                for record in extract_records(
                    result.entries, encounter_record_from
                )
                if record.start >= tool_input.since
            ),
            key=_encounter_sort_key,
            reverse=True,
        )
    )
    truncated = result.truncated or len(eligible) > cap
    records = eligible[:cap]
    if records:
        return GetEncountersSinceOutput(records=records, truncated=truncated)
    return GetEncountersSinceOutput(
        records=(),
        truncated=truncated,
        receipt=_receipt(
            "Encounter",
            params,
            f"encounters on or after {tool_input.since.isoformat()} for "
            f"patient {tool_input.patient_id}",
        ),
    )


def _encounter_sort_key(record: EncounterRecord) -> datetime:
    return record.start


async def search_documents(
    tool_input: SearchDocumentsInput, client: FhirClient
) -> SearchDocumentsOutput:
    """Search document references — metadata refs only, never content."""
    params: Params = {"patient": tool_input.patient_id}
    _date_range_params(params, tool_input.start, tool_input.end)
    result = await client.search("DocumentReference", params)
    records = extract_records(result.entries, document_record_from)
    if records:
        return SearchDocumentsOutput(records=records)
    return SearchDocumentsOutput(
        records=(),
        receipt=_receipt(
            "DocumentReference",
            params,
            f"document references matching the requested filters for "
            f"patient {tool_input.patient_id}",
        ),
    )


async def get_immunizations(
    tool_input: GetImmunizationsInput, client: FhirClient
) -> GetImmunizationsOutput:
    """Fetch all immunization records for the patient."""
    params: Params = {"patient": tool_input.patient_id}
    result = await client.search("Immunization", params)
    records = extract_records(result.entries, immunization_record_from)
    if records:
        return GetImmunizationsOutput(records=records)
    return GetImmunizationsOutput(
        records=(),
        receipt=_receipt(
            "Immunization",
            params,
            f"all immunization records for patient {tool_input.patient_id}",
        ),
    )
