"""Medication reconciliation contracts (T007).

OpenEMR medications live in multiple tables (``prescriptions``, ``lists``)
that can disagree — AUDIT.md D3/D4, e.g. patient 2's Lisinopril is active
in ``prescriptions`` but inactive in ``lists``. These frozen value objects
model the *result* of reconciling records from both sources: one entry per
drug, citing every contributing source, with disagreements surfaced as an
explicit :class:`MedicationConflict` rather than silently resolved
(ARCHITECTURE.md §8 status-filtering boundary).

The reconciler itself is a pure function over the T002 ``MedicationRecord``
contract; it lives in :mod:`copilot.tools.reconciliation`.
"""

from enum import Enum

from pydantic import Field

from copilot.contracts.base import ContractModel
from copilot.contracts.refs import ResourceRef


class InteractionCheck(str, Enum):
    """Whether a drug-interaction check could even be attempted.

    Hot-path interaction flags arrive with the CDS integration (out of
    scope here). What matters now is that an uncoded medication yields an
    explicit "unavailable" marker, never a silent clean pass
    (ARCHITECTURE.md §11.6, AUDIT.md D3).
    """

    NOT_RUN = "not_run"
    UNAVAILABLE_UNCODED = "unavailable_uncoded"


class SourceStatus(ContractModel):
    """One source's contribution to a reconciled drug: its raw status.

    ``active`` is the interpreted current-vs-resolved reading of the raw
    ``status`` string (ARCHITECTURE.md §8), kept alongside the raw value so
    the interpretation is auditable and never lossy.
    """

    source: str = Field(min_length=1)
    status: str | None = None
    active: bool


class MedicationConflict(ContractModel):
    """Sources disagree on whether the same drug is active.

    Carries every disagreeing source with its status so the conflict names
    both sides explicitly — it is never resolved to one winning status.
    """

    sources: tuple[SourceStatus, ...] = Field(min_length=2)


class ReconciledMedication(ContractModel):
    """One drug reconciled across every source that recorded it.

    ``refs`` cites *all* contributing source resources so the merged entry
    stays traceable to each. ``is_current`` is true when the drug is active
    in at least one source (an active-in-any med stays current and, if
    sources disagree, is additionally flagged rather than hidden).
    """

    medication: str = Field(min_length=1)
    normalized_name: str = Field(min_length=1)
    rxnorm: str | None = None
    refs: tuple[ResourceRef, ...] = Field(min_length=1)
    sources: tuple[SourceStatus, ...] = Field(min_length=1)
    is_current: bool
    interaction_check: InteractionCheck
    conflict: MedicationConflict | None = None


class MedicationReconciliation(ContractModel):
    """Reconciled medication view: current meds split from historical.

    Records inactive in *all* sources are excluded from ``current`` and
    surfaced in ``historical`` — resolved/discontinued meds must never
    appear as current (ARCHITECTURE.md §8).
    """

    current: tuple[ReconciledMedication, ...] = ()
    historical: tuple[ReconciledMedication, ...] = ()
