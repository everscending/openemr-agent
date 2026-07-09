"""Typed contracts for tool I/O, citations, and coverage (T002).

These pydantic models are the source of truth for every tool boundary:
frozen, strictly validated, and JSON round-trippable.
"""

from copilot.contracts.base import ContractModel
from copilot.contracts.coverage import (
    CategoryCoverage,
    CoverageOk,
    CoverageUnavailable,
    CoverageVerifiedEmpty,
)
from copilot.contracts.reconciliation import (
    InteractionCheck,
    MedicationConflict,
    MedicationReconciliation,
    ReconciledMedication,
    SourceStatus,
)
from copilot.contracts.refs import FhirResourceType, ResourceRef
from copilot.contracts.tools import (
    AllergyRecord,
    ConditionRecord,
    DateRangeMixin,
    DocumentRecord,
    EncounterRecord,
    GetEncountersSinceInput,
    GetEncountersSinceOutput,
    GetImmunizationsInput,
    GetImmunizationsOutput,
    GetMedicationHistoryInput,
    GetMedicationHistoryOutput,
    GetPatientSnapshotInput,
    ImmunizationRecord,
    MedicationRecord,
    ObservationRecord,
    OutputRecord,
    PatientRecord,
    PatientSnapshotOutput,
    QueryReceipt,
    SearchDocumentsInput,
    SearchDocumentsOutput,
    SearchObservationsInput,
    SearchObservationsOutput,
    ToolInput,
)

__all__ = [
    "AllergyRecord",
    "CategoryCoverage",
    "ConditionRecord",
    "ContractModel",
    "CoverageOk",
    "CoverageUnavailable",
    "CoverageVerifiedEmpty",
    "DateRangeMixin",
    "DocumentRecord",
    "EncounterRecord",
    "FhirResourceType",
    "GetEncountersSinceInput",
    "GetEncountersSinceOutput",
    "GetImmunizationsInput",
    "GetImmunizationsOutput",
    "GetMedicationHistoryInput",
    "GetMedicationHistoryOutput",
    "GetPatientSnapshotInput",
    "ImmunizationRecord",
    "InteractionCheck",
    "MedicationConflict",
    "MedicationReconciliation",
    "MedicationRecord",
    "ObservationRecord",
    "ReconciledMedication",
    "SourceStatus",
    "OutputRecord",
    "PatientRecord",
    "PatientSnapshotOutput",
    "QueryReceipt",
    "ResourceRef",
    "SearchDocumentsInput",
    "SearchDocumentsOutput",
    "SearchObservationsInput",
    "SearchObservationsOutput",
    "ToolInput",
]
