"""Tool input/output contracts.

One input and one output model per tool in ARCHITECTURE.md section 2:
``get_patient_snapshot``, ``search_observations``,
``get_medication_history``, ``get_encounters_since``,
``search_documents``, ``get_immunizations``.

Every input requires a ``patient_id``; date-range inputs reject
``start > end``. Every output record embeds a ``ResourceRef`` so each
datum is citable back to its source FHIR resource.
"""

from datetime import date

from pydantic import AwareDatetime, Field, model_validator

from copilot.contracts.base import ContractModel
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


class GetMedicationHistoryInput(ToolInput):
    """Input for ``get_medication_history``."""


class GetEncountersSinceInput(ToolInput):
    """Input for ``get_encounters_since``."""

    since: AwareDatetime


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
    """A medication request/history entry."""

    medication: str = Field(min_length=1)
    status: str | None = None


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


class PatientSnapshotOutput(ContractModel):
    """Output of ``get_patient_snapshot``."""

    patient: PatientRecord
    conditions: tuple[ConditionRecord, ...] = ()
    allergies: tuple[AllergyRecord, ...] = ()


class SearchObservationsOutput(ContractModel):
    """Output of ``search_observations``."""

    records: tuple[ObservationRecord, ...]


class GetMedicationHistoryOutput(ContractModel):
    """Output of ``get_medication_history``."""

    records: tuple[MedicationRecord, ...]


class GetEncountersSinceOutput(ContractModel):
    """Output of ``get_encounters_since``."""

    records: tuple[EncounterRecord, ...]


class SearchDocumentsOutput(ContractModel):
    """Output of ``search_documents``."""

    records: tuple[DocumentRecord, ...]


class GetImmunizationsOutput(ContractModel):
    """Output of ``get_immunizations``."""

    records: tuple[ImmunizationRecord, ...]
