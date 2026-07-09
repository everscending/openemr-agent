"""Tool input/output contracts.

One input and one output model per tool in ARCHITECTURE.md section 2:
``get_patient_snapshot``, ``search_observations``,
``get_medication_history``, ``get_encounters_since``,
``search_documents``, ``get_immunizations``.

Every input requires a ``patient_id``; date-range inputs reject
``start > end``. Every output record embeds a ``ResourceRef`` so each
datum is citable back to its source FHIR resource.
"""

from datetime import date, datetime, timezone

from pydantic import AwareDatetime, Field, model_validator

from copilot.contracts.base import ContractModel
from copilot.contracts.coverage import CategoryCoverage
from copilot.contracts.refs import ResourceRef

# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


class ToolInput(ContractModel):
    """Base for all tool inputs: every tool is patient-scoped."""

    patient_id: str = Field(min_length=1)


class DateRangeMixin(ContractModel):
    """Optional inclusive date range; rejects ``start > end``."""

    start: AwareDatetime | None = None
    end: AwareDatetime | None = None

    @model_validator(mode="after")
    def _check_range(self) -> "DateRangeMixin":
        if self.start is not None and self.end is not None and self.start > self.end:
            raise ValueError("start must not be after end")
        return self


class GetPatientSnapshotInput(ToolInput):
    """Input for ``get_patient_snapshot``."""


class SearchObservationsInput(ToolInput, DateRangeMixin):
    """Input for ``search_observations``."""

    code: str | None = None
    category: str | None = None


class GetMedicationHistoryInput(ToolInput):
    """Input for ``get_medication_history``."""


class GetEncountersSinceInput(ToolInput):
    """Input for ``get_encounters_since``.

    ``since`` is a reference point in the past — "what happened since my
    last visit" (USER.md UC-3). A future reference date can only be a
    caller mistake, so it is rejected at the boundary.
    """

    since: AwareDatetime

    @model_validator(mode="after")
    def _reject_future_reference(self) -> "GetEncountersSinceInput":
        if self.since > datetime.now(timezone.utc):
            raise ValueError("since must not be in the future")
        return self


class SearchDocumentsInput(ToolInput, DateRangeMixin):
    """Input for ``search_documents``."""

    query: str | None = None


class GetImmunizationsInput(ToolInput):
    """Input for ``get_immunizations``."""


# ---------------------------------------------------------------------------
# Output records (each embeds a ResourceRef)
# ---------------------------------------------------------------------------


class OutputRecord(ContractModel):
    """Base for all tool-output records: always citable via ``ref``."""

    ref: ResourceRef


class PatientRecord(OutputRecord):
    """Patient demographics."""

    name: str = Field(min_length=1)
    birth_date: date | None = None


class ConditionRecord(OutputRecord):
    """An active or historical condition."""

    display: str = Field(min_length=1)


class AllergyRecord(OutputRecord):
    """An allergy or intolerance."""

    display: str = Field(min_length=1)


class ObservationRecord(OutputRecord):
    """A clinical observation (lab, vital, etc.)."""

    code: str = Field(min_length=1)
    display: str = Field(min_length=1)
    value: str | None = None
    effective: AwareDatetime | None = None


class MedicationRecord(OutputRecord):
    """A medication request/history entry.

    ``source`` names which FHIR-exposed medication list the record came
    from (e.g. ``"prescriptions"``) so downstream consumers can tell the
    two OpenEMR medication sources apart (reconciliation itself is T007).
    """

    medication: str = Field(min_length=1)
    status: str | None = None
    source: str | None = None


class EncounterRecord(OutputRecord):
    """A clinical encounter."""

    start: AwareDatetime
    reason: str | None = None


class DocumentRecord(OutputRecord):
    """A clinical document reference."""

    title: str = Field(min_length=1)
    created: AwareDatetime | None = None


class ImmunizationRecord(OutputRecord):
    """An administered immunization."""

    vaccine: str = Field(min_length=1)
    administered: AwareDatetime | None = None


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


class QueryReceipt(ContractModel):
    """Proof of a search that returned nothing (USER.md UC-4).

    A negative answer must state what was searched — the exact query, its
    scope, and when it ran — never a bare "no". Targeted-tool outputs carry
    a receipt exactly when they return zero records.
    """

    query_description: str = Field(min_length=1)
    scope: str = Field(min_length=1)
    timestamp: AwareDatetime


class PatientSnapshotOutput(ContractModel):
    """Output of ``get_patient_snapshot``.

    ``patient`` is ``None`` when the demographics fetch itself failed —
    that failure is reported in ``coverage`` rather than failing the whole
    snapshot (graceful degradation, ARCHITECTURE.md section 7). ``coverage``
    carries one entry per snapshot category on every call so the physician
    always knows what was and wasn't checked.
    """

    patient: PatientRecord | None = None
    conditions: tuple[ConditionRecord, ...] = ()
    allergies: tuple[AllergyRecord, ...] = ()
    medications: tuple[MedicationRecord, ...] = ()
    labs: tuple[ObservationRecord, ...] = ()
    last_encounter: EncounterRecord | None = None
    coverage: tuple[CategoryCoverage, ...] = ()


class SearchObservationsOutput(ContractModel):
    """Output of ``search_observations``.

    ``receipt`` is present exactly when ``records`` is empty (UC-4).
    """

    records: tuple[ObservationRecord, ...]
    receipt: QueryReceipt | None = None


class GetMedicationHistoryOutput(ContractModel):
    """Output of ``get_medication_history``.

    ``receipt`` is present exactly when ``records`` is empty (UC-4).
    """

    records: tuple[MedicationRecord, ...]
    receipt: QueryReceipt | None = None


class GetEncountersSinceOutput(ContractModel):
    """Output of ``get_encounters_since``.

    ``truncated`` is True when the batch cap was hit — the cap is surfaced,
    never silent (ARCHITECTURE.md section 6d). ``receipt`` is present
    exactly when ``records`` is empty (UC-4).
    """

    records: tuple[EncounterRecord, ...]
    truncated: bool = False
    receipt: QueryReceipt | None = None


class SearchDocumentsOutput(ContractModel):
    """Output of ``search_documents``.

    ``receipt`` is present exactly when ``records`` is empty (UC-4).
    """

    records: tuple[DocumentRecord, ...]
    receipt: QueryReceipt | None = None


class GetImmunizationsOutput(ContractModel):
    """Output of ``get_immunizations``.

    ``receipt`` is present exactly when ``records`` is empty (UC-4).
    """

    records: tuple[ImmunizationRecord, ...]
    receipt: QueryReceipt | None = None
