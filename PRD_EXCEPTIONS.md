# PRD_EXCEPTIONS.md — Requirements Deliberately Not Fully Implemented

The PRD's own standard is that every architectural decision be traceable back
to it, and that tradeoffs be deliberate and defensible. This document is the
ledger for the other direction: **every place a PRD requirement is deliberately
not fully implemented, why, what stands in its place, and the path to full
implementation.** It exists so no gap is silent — each entry is a decision with
an owner and a date, not an omission.

Maintenance rule: any future decision that leaves a PRD requirement partially
implemented for a valid reason gets an entry here, at the time the decision is
made. Companion documents: `ARCHITECTURE.md` §11 (known limitations of the
design as built) and `.tdd-swarm/progress.md` (the ticket ledger).

---

## E1 — Drug–drug interaction checking (domain constraint enforcement)

**Date / decided by:** 2026-07-11, project owner (with orchestrator
verification probes).

**PRD requirement (Verification & Trust; Agent Requirements — Verification
System):** "the agent must be aware of domain constraints: clinical rules,
dosage thresholds, interaction flags... The requirement is that you have one,
that it is deliberate, and that you can defend it."

**What is implemented instead (the deliberate approach):**
- **Refusal boundary** (USER.md §4): dosing, treatment, and diagnosis requests
  are refused — the agent retrieves and cites; it does not practice medicine.
- **Deterministic value verification** (T008/T009): every clinical claim must
  cite a real resource from this request's tool results, and parseable
  numerics/dates in claims are compared against the cited resource's actual
  fields — a response that violates what the underlying data says is stripped,
  fail-closed. This directly implements the PRD's "a response that violates
  what the underlying data actually says is a failure."
- **Medication conflict flagging** (T007): medications are reconciled across
  OpenEMR's disagreeing source tables; status conflicts are flagged, never
  silently resolved.
- **Explicit unchecked markers** (T007): a medication whose interaction status
  could not be checked carries `unavailable_uncoded` / `not_run` — never a
  silent clean pass.
- **Drug–allergy cross-check** (T033): a deterministic, name-level check
  flagging any current medication matching a recorded allergy — an
  interaction-class constraint enforced in code, attributed to the data, with
  its limitation (no cross-reactivity/drug-class knowledge) disclosed in its
  own status vocabulary.

**Why full drug–drug checking is not implemented (verified 2026-07-11):**
OpenEMR's only native drug–drug check (`controllers/C_Prescription.class.php:190-231`)
is unusable end-to-end:
1. It calls the NLM RxNav Interaction API, which **NLM retired in January
   2024** — a live probe returns HTTP 404. There is no drop-in free
   replacement; the retired API's data came from ONCHigh + DrugBank.
2. On that failure the native code **renders "No interactions found"** — a
   silent clean pass on a dead upstream, the exact failure mode this project's
   verification invariants exist to prevent. (Recorded as an audit-class
   finding; it independently justifies why the agent marks interaction checks
   "not run" rather than trusting the platform's output.)
3. It requires a locally installed RxNorm dataset (`RXNCONSO` — not installed)
   solely to map free-text names to CUIs, which it does by `LIKE`-matching the
   first word of the drug name, ignoring the `rxnorm_drugcode` column.

A real implementation therefore requires sourcing and licensing an interaction
knowledge base (ONC high-priority DDI list, DrugBank, or equivalent) — new
scope that cannot be responsibly delivered inside this sprint.

**Note on the data (supersedes AUDIT.md D3 for the current dataset):** the
audit's finding that medications are uncoded was true of the original
3-patient demo set but is stale after the Synthea seed: 689 `prescriptions`
rows (~91% carry real RxNorm CUIs), 633 `lists` medications (~99% coded). The
blocker is the knowledge source, not the data.

**Path to full implementation:** ticket **T034** (deferred) — probe-first
selection of an interaction knowledge source, pairwise CUI checking with the
failure direction fixed (source failure ⇒ "unavailable," never "no
interactions found"), and closure or narrowing of this entry.

---

*(E2 — per-user identity / audit attribution on agent requests — CLOSED
2026-07-11 by ticket T027, which replaced T021's service-account bearer with a
per-clinician, per-patient SMART token minted server-side. FHIR reads now
execute patient-scoped and the audit/§164.528 rows name the acting clinician.
This ledger tracks only open exceptions, so the entry was removed; see the
T027 commit and the TDD ledger for the record.)*

---

*Future entries are appended below in the same format: requirement, what is
implemented instead, why, and the path to full implementation.*
