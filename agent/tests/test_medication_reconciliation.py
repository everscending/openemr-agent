"""Tests for medication reconciliation and conflict flagging (T007).

AUDIT.md D3/D4, ARCHITECTURE.md §8/§11.6: OpenEMR medications live in
multiple tables that can disagree. The reconciler is a *pure* function
over the T002 ``MedicationRecord`` contract (each tagged with its source)
that merges records referring to the same drug, flags active-status
conflicts instead of silently resolving them, splits current from
historical, and marks uncoded meds as un-interaction-checkable.

Criteria map:
  1. Same drug from both sources merges into one entry citing BOTH refs.
  2. Disagreement on active status => explicit ``conflict`` naming both
     sources and their statuses; never resolved to one status silently.
     (Pinned Lisinopril case.)
  3. Inactive-in-all records are historical, never surfaced as current.
  4. Uncoded med => ``interaction_check = unavailable_uncoded``; coded med
     => ``interaction_check = not_run``; the distinction is representable.
  5. Conservative matching: identical normalized names OR identical RxNorm
     codes merge; two genuinely different drugs are never merged.
  6. The T005 snapshot's medication category uses this reconciler
     (integration through the snapshot path with a conflicting fixture).

Production code is imported lazily inside tests so collection succeeds
before the implementation exists (RED = the missing feature, per test).
"""

from __future__ import annotations

from typing import Any

from copilot import contracts

PATIENT_ID = "pat-2"
TOKEN = "user-token"

PRESCRIPTIONS = "prescriptions"
LISTS = "lists"


# ---------------------------------------------------------------------------
# Lazy module accessors
# ---------------------------------------------------------------------------


def recon_mod() -> Any:
    from copilot.tools import reconciliation

    return reconciliation


def snapshot_mod() -> Any:
    from copilot.tools import snapshot

    return snapshot


# ---------------------------------------------------------------------------
# Record builders (called inside test bodies, never at collection time)
# ---------------------------------------------------------------------------


def make_ref(resource_id: str) -> Any:
    return contracts.ResourceRef(
        resource_type="MedicationRequest", resource_id=resource_id
    )


def med_record(
    resource_id: str,
    medication: str,
    *,
    status: str | None,
    source: str,
    rxnorm: str | None = None,
) -> Any:
    return contracts.MedicationRecord(
        ref=make_ref(resource_id),
        medication=medication,
        status=status,
        source=source,
        rxnorm=rxnorm,
    )


def lisinopril_conflict_records() -> tuple[Any, Any]:
    """Patient 2's real conflict: active in prescriptions, inactive in lists."""
    return (
        med_record(
            "rx-lisinopril",
            "Lisinopril",
            status="active",
            source=PRESCRIPTIONS,
        ),
        med_record(
            "list-lisinopril",
            "Lisinopril",
            status="inactive",
            source=LISTS,
        ),
    )


def reconcile(records: Any) -> Any:
    return recon_mod().reconcile_medications(records)


def only(items: Any) -> Any:
    seq = tuple(items)
    assert len(seq) == 1, f"expected exactly one item, got {len(seq)}"
    return seq[0]


# ==========================================================================
# Criterion 1 — same drug from both sources merges, citing both refs
# ==========================================================================


def test_same_drug_from_both_sources_merges_into_one_entry() -> None:
    result = reconcile(lisinopril_conflict_records())

    assert isinstance(result, contracts.MedicationReconciliation)
    merged = only(result.current + result.historical)
    # One reconciled entry represents the single drug across two sources.
    assert len(merged.refs) == 2


def test_merged_entry_cites_both_source_refs() -> None:
    rx, lst = lisinopril_conflict_records()
    result = reconcile((rx, lst))

    merged = only(result.current + result.historical)
    cited_ids = {ref.resource_id for ref in merged.refs}
    assert cited_ids == {"rx-lisinopril", "list-lisinopril"}
    # Both originating sources are named on the merged entry.
    named_sources = {s.source for s in merged.sources}
    assert named_sources == {PRESCRIPTIONS, LISTS}


# ==========================================================================
# Criterion 2 — status disagreement => explicit conflict, never resolved
# ==========================================================================


def test_lisinopril_active_in_prescriptions_inactive_in_lists_flags_conflict() -> None:
    """Pinned: patient 2's Lisinopril conflict must be flagged, not resolved."""
    result = reconcile(lisinopril_conflict_records())

    merged = only(result.current + result.historical)
    assert merged.conflict is not None, "the source disagreement was not flagged"

    # The conflict names BOTH sources with their respective statuses.
    conflict_map = {s.source: s.status for s in merged.conflict.sources}
    assert conflict_map == {PRESCRIPTIONS: "active", LISTS: "inactive"}


def test_conflict_is_not_resolved_to_a_single_status_silently() -> None:
    result = reconcile(lisinopril_conflict_records())
    merged = only(result.current + result.historical)

    # Both source statuses remain visible on the entry — the reconciler did
    # not collapse the disagreement into one winning status.
    statuses_by_source = {s.source: s.status for s in merged.sources}
    assert statuses_by_source == {PRESCRIPTIONS: "active", LISTS: "inactive"}
    assert merged.conflict is not None


def test_agreeing_sources_do_not_flag_a_conflict() -> None:
    records = (
        med_record("rx-1", "Metformin", status="active", source=PRESCRIPTIONS),
        med_record("list-1", "Metformin", status="active", source=LISTS),
    )
    merged = only(reconcile(records).current)
    assert merged.conflict is None


# ==========================================================================
# Criterion 3 — inactive-in-all is historical, never current
# ==========================================================================


def test_inactive_in_all_sources_is_historical_not_current() -> None:
    records = (
        med_record("rx-1", "Lipitor", status="inactive", source=PRESCRIPTIONS),
        med_record("list-1", "Lipitor", status="inactive", source=LISTS),
    )
    result = reconcile(records)

    assert result.current == ()
    merged = only(result.historical)
    assert merged.is_current is False
    assert {ref.resource_id for ref in merged.refs} == {"rx-1", "list-1"}


def test_active_med_is_current_not_historical() -> None:
    records = (med_record("rx-1", "Norvasc", status="active", source=PRESCRIPTIONS),)
    result = reconcile(records)

    assert result.historical == ()
    merged = only(result.current)
    assert merged.is_current is True


def test_conflicting_med_active_in_one_source_stays_in_current() -> None:
    # Safety: a med active in *any* source must remain current (and flagged),
    # never hidden in history by the disagreement.
    result = reconcile(lisinopril_conflict_records())
    merged = only(result.current)
    assert merged.is_current is True
    assert merged.conflict is not None
    assert result.historical == ()


# ==========================================================================
# Criterion 4 — interaction_check: uncoded vs coded is representable
# ==========================================================================


def test_uncoded_medication_marks_interaction_check_unavailable() -> None:
    records = (
        med_record("rx-1", "Lisinopril", status="active", source=PRESCRIPTIONS, rxnorm=None),
    )
    merged = only(reconcile(records).current)
    assert merged.interaction_check == contracts.InteractionCheck.UNAVAILABLE_UNCODED


def test_empty_string_rxnorm_counts_as_uncoded() -> None:
    records = (
        med_record("rx-1", "Lisinopril", status="active", source=PRESCRIPTIONS, rxnorm="   "),
    )
    merged = only(reconcile(records).current)
    assert merged.interaction_check == contracts.InteractionCheck.UNAVAILABLE_UNCODED


def test_coded_medication_marks_interaction_check_not_run() -> None:
    records = (
        med_record(
            "rx-1", "Lisinopril", status="active", source=PRESCRIPTIONS, rxnorm="29046"
        ),
    )
    merged = only(reconcile(records).current)
    assert merged.interaction_check == contracts.InteractionCheck.NOT_RUN


def test_coded_and_uncoded_are_distinct_values() -> None:
    assert (
        contracts.InteractionCheck.NOT_RUN
        != contracts.InteractionCheck.UNAVAILABLE_UNCODED
    )


# ==========================================================================
# Criterion 5 — conservative matching
# ==========================================================================


def test_identical_rxnorm_codes_merge_even_with_different_names() -> None:
    # Same RxNorm code means the same drug even if the free-text differs
    # (brand vs generic). This is an exact-code merge, not fuzzy matching.
    records = (
        med_record("rx-1", "Zestril", status="active", source=PRESCRIPTIONS, rxnorm="29046"),
        med_record("list-1", "Lisinopril", status="active", source=LISTS, rxnorm="29046"),
    )
    result = reconcile(records)
    merged = only(result.current)
    assert len(merged.refs) == 2


def test_identical_normalized_names_merge_case_and_whitespace_folded() -> None:
    records = (
        med_record("rx-1", "  LISINOPRIL  ", status="active", source=PRESCRIPTIONS),
        med_record("list-1", "lisinopril", status="active", source=LISTS),
    )
    result = reconcile(records)
    merged = only(result.current)
    assert len(merged.refs) == 2


def test_two_different_drugs_are_never_merged() -> None:
    records = (
        med_record("rx-1", "Lisinopril", status="active", source=PRESCRIPTIONS, rxnorm="29046"),
        med_record("rx-2", "Metformin", status="active", source=PRESCRIPTIONS, rxnorm="6809"),
    )
    result = reconcile(records)

    assert len(result.current) == 2
    meds = {m.normalized_name for m in result.current}
    assert meds == {"lisinopril", "metformin"}
    for m in result.current:
        assert len(m.refs) == 1


def test_different_names_and_no_codes_stay_separate() -> None:
    records = (
        med_record("rx-1", "Aspirin", status="active", source=PRESCRIPTIONS),
        med_record("rx-2", "Warfarin", status="active", source=PRESCRIPTIONS),
    )
    result = reconcile(records)
    assert len(result.current) == 2


# ==========================================================================
# Criterion 6 — the snapshot path runs medications through the reconciler
# ==========================================================================


async def _snapshot_with_medications(mod: Any, med_records: tuple[Any, ...]) -> Any:
    async def med_fetcher(pid: str) -> Any:
        return mod.CategoryFetchResult(
            records=med_records,
            query_description=f"medications for {pid}",
            scope=f"medications for {pid}",
        )

    async def empty_fetcher(pid: str) -> Any:
        return mod.CategoryFetchResult(
            records=(), query_description="q", scope="s"
        )

    fetchers = mod.SnapshotFetchers(
        demographics=empty_fetcher,
        medications=med_fetcher,
        problems=empty_fetcher,
        allergies=empty_fetcher,
        labs=empty_fetcher,
        last_encounter=empty_fetcher,
    )
    return await mod.get_patient_snapshot(PATIENT_ID, TOKEN, fetchers=fetchers)


async def test_snapshot_medication_category_reconciles_and_flags_conflict() -> None:
    mod = snapshot_mod()
    result = await _snapshot_with_medications(mod, lisinopril_conflict_records())

    assert isinstance(result, contracts.PatientSnapshotOutput)
    reconciliation = result.medication_reconciliation
    assert reconciliation is not None, "snapshot did not run the reconciler"
    assert isinstance(reconciliation, contracts.MedicationReconciliation)

    merged = only(reconciliation.current)
    assert merged.conflict is not None, "snapshot path did not flag the conflict"
    assert {s.source for s in merged.conflict.sources} == {PRESCRIPTIONS, LISTS}
    # A single reconciled entry, not two raw rows.
    assert len(merged.refs) == 2


async def test_snapshot_reconciler_excludes_inactive_from_current() -> None:
    mod = snapshot_mod()
    records = (
        med_record("rx-1", "Lipitor", status="inactive", source=PRESCRIPTIONS),
        med_record("list-1", "Lipitor", status="inactive", source=LISTS),
    )
    result = await _snapshot_with_medications(mod, records)

    reconciliation = result.medication_reconciliation
    assert reconciliation is not None
    assert reconciliation.current == ()
    assert len(reconciliation.historical) == 1
