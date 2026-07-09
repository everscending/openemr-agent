"""Medication reconciliation and conflict flagging (T007).

A pure function over the T002 ``MedicationRecord`` contract — no I/O, no
LLM — that reconciles medications drawn from OpenEMR's disagreeing sources
(``prescriptions``, ``lists``; AUDIT.md D3/D4). Records that refer to the
same drug are merged into one entry citing every contributing source; when
the sources disagree on active status the merged entry is *flagged*, never
silently resolved to one status. Meds inactive in all sources are split off
into the historical view so resolved/discontinued drugs never surface as
current (ARCHITECTURE.md §8). Uncoded meds carry an explicit
"interaction check unavailable" marker (ARCHITECTURE.md §11.6).

Matching is deliberately conservative (out of scope: fuzzy name matching —
false merges are worse than missed ones): two records merge only when their
normalized (case/whitespace-folded) names are identical or their RxNorm
codes are identical. Anything else stays separate.
"""

from __future__ import annotations

from typing import Iterable

from copilot.contracts.reconciliation import (
    InteractionCheck,
    MedicationConflict,
    MedicationReconciliation,
    ReconciledMedication,
    SourceStatus,
)
from copilot.contracts.tools import MedicationRecord

# Raw status strings interpreted as "currently active". Everything else —
# inactive, stopped, completed, resolved, discontinued, unknown, or absent —
# is read as not current (ARCHITECTURE.md §8: conservative status filtering).
_ACTIVE_STATUSES = frozenset({"active"})


def reconcile_medications(
    records: Iterable[MedicationRecord],
) -> MedicationReconciliation:
    """Reconcile medication records from multiple sources into one view.

    Merges same-drug records (by normalized name or shared RxNorm code),
    flags active-status disagreements, and splits current from historical.
    """
    items = list(records)
    parent = list(range(len(items)))

    def find(node: int) -> int:
        root = node
        while parent[root] != root:
            root = parent[root]
        while parent[node] != root:
            parent[node], node = root, parent[node]
        return root

    def union(left: int, right: int) -> None:
        parent[find(left)] = find(right)

    by_name: dict[str, int] = {}
    by_code: dict[str, int] = {}
    for idx, record in enumerate(items):
        name_key = _normalize_name(record.medication)
        if name_key in by_name:
            union(idx, by_name[name_key])
        else:
            by_name[name_key] = idx

        code_key = _normalize_code(record.rxnorm)
        if code_key is not None:
            if code_key in by_code:
                union(idx, by_code[code_key])
            else:
                by_code[code_key] = idx

    groups: dict[int, list[MedicationRecord]] = {}
    for idx, record in enumerate(items):
        groups.setdefault(find(idx), []).append(record)

    reconciled = [_merge_group(group) for group in groups.values()]
    current = tuple(entry for entry in reconciled if entry.is_current)
    historical = tuple(entry for entry in reconciled if not entry.is_current)
    return MedicationReconciliation(current=current, historical=historical)


def _merge_group(group: list[MedicationRecord]) -> ReconciledMedication:
    """Collapse one same-drug group into a single reconciled entry."""
    first = group[0]
    rxnorm = next(
        (
            _normalize_code(record.rxnorm)
            for record in group
            if _normalize_code(record.rxnorm) is not None
        ),
        None,
    )
    sources = tuple(
        SourceStatus(
            source=record.source or "unknown",
            status=record.status,
            active=_is_active(record.status),
        )
        for record in group
    )
    is_current = any(source.active for source in sources)
    disagrees = is_current and any(not source.active for source in sources)
    conflict = MedicationConflict(sources=sources) if disagrees else None
    interaction_check = (
        InteractionCheck.NOT_RUN
        if rxnorm is not None
        else InteractionCheck.UNAVAILABLE_UNCODED
    )
    return ReconciledMedication(
        medication=first.medication,
        normalized_name=_normalize_name(first.medication),
        rxnorm=rxnorm,
        refs=tuple(record.ref for record in group),
        sources=sources,
        is_current=is_current,
        interaction_check=interaction_check,
        conflict=conflict,
    )


def _normalize_name(name: str) -> str:
    """Case- and whitespace-fold a drug name for conservative matching."""
    return " ".join(name.split()).casefold()


def _normalize_code(code: str | None) -> str | None:
    """A non-empty, whitespace-trimmed RxNorm code, or ``None`` if absent."""
    if code is None:
        return None
    trimmed = code.strip()
    return trimmed or None


def _is_active(status: str | None) -> bool:
    if status is None:
        return False
    return status.strip().casefold() in _ACTIVE_STATUSES
