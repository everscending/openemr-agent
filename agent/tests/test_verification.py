"""Verification layer — citation grounding, fail closed (T008, rev 3).

ARCHITECTURE.md §5 Layer 1 (rewritten): every clinical claim in model output
must cite ``[ResourceType/id]``; a deterministic, non-LLM post-processor
verifies every cited ID appeared in this request's tool results (keyed by
**type + id**). Uncited or falsely-cited claims fail closed.

Fail-closed rule (no fractional floor): if at least one claim-bearing sentence
survives, the survivors are emitted with the removal annotation. Only when
**no** claim-bearing sentence survives *and* at least one claim was stripped is
the whole response replaced by the fallback text. A draft that made no claims
at all (pure scaffolding) is never a fallback case. There is no threshold
parameter, no configurable fraction, and no ``DEFAULT_FALLBACK_THRESHOLD``-style
constant. Adding scaffolding to a draft must never change the fallback decision.

Criteria map:
  1. Deterministic verifier over (draft text, set of ``ResourceRef``s) returns a
     structured verdict (verified text + per-claim outcomes + counts). No
     LLM/network imports — asserted structurally by AST-parsing the module.
  2. A claim citing an ID in the tool-result set passes; its token is preserved
     in the output for panel-side rendering.
  3. A claim citing an ID NOT in the set is stripped and annotated
     (machine-readable stripped list + user-visible removal marker), and the
     available ``ResourceRef``s are attached on the stripped (non-fallback)
     path.
  4. A sentence making a clinical claim with NO citation is stripped and
     annotated; whitelisted scaffolding (greeting/coverage/absence/refusal/
     navigation/header) survives without a citation. THE BOUNDARY (rev 3):
     scaffolding is a statement about the *agent's process*; a clinical claim is
     a statement about *the patient*. The coverage/absence whitelist admits only
     a retrieval verb / record-marker over a **data category**, never an
     arbitrary proposition. ``verified`` and ``confirmed`` are assertive, NOT
     coverage verbs. The adversarial block below proves the whitelist does not
     *over*-match — the exact hole that sank rev 2.
  5. No fractional threshold. one-survives-one-stripped => annotated partial, no
     fallback; every claim stripped (incl. a single-claim draft) => full
     fallback with ``output_text`` exactly the fallback text; pure scaffolding
     => no fallback, nothing stripped. Anti-dilution: prepending scaffolding to
     an all-stripped draft never changes the fallback decision. No threshold
     parameter/constant exists.
  6. Type mismatch fails: citing ``[Observation/xyz]`` when ``xyz`` was returned
     as an Encounter is a failed citation (set keyed by type+id).
  7. Verdict counts (total / passed / stripped) present and correct — even when
     the fallback triggers.

Production code is imported lazily inside test bodies so collection succeeds
before the implementation exists (RED = the missing feature per test).
"""

from __future__ import annotations

import ast
import inspect
from typing import Any

import pytest

from copilot import contracts

# ---------------------------------------------------------------------------
# Lazy accessors (called inside bodies, never at collection time)
# ---------------------------------------------------------------------------


def verify_mod() -> Any:
    from copilot import verification

    return verification


def verify(draft: str, refs: Any, **kwargs: Any) -> Any:
    return verify_mod().verify_response(draft, refs, **kwargs)


def ref(resource_type: str, resource_id: str) -> Any:
    return contracts.ResourceRef(
        resource_type=resource_type, resource_id=resource_id
    )


def token(resource_type: str, resource_id: str) -> str:
    return f"[{resource_type}/{resource_id}]"


VERIFIED = "verified"
SCAFFOLD = "scaffolding"
STRIP_STATUSES = {
    "stripped_uncited",
    "stripped_unknown_citation",
    "stripped_type_mismatch",
}


def status_value(claim: Any) -> str:
    """A claim's status as its plain string value (enum-agnostic)."""
    status = claim.status
    return getattr(status, "value", status)


def only_claim(verdict: Any) -> Any:
    """The single claim-bearing verdict (excludes scaffolding)."""
    claims = [v for v in verdict.verdicts if status_value(v) != SCAFFOLD]
    assert len(claims) == 1, f"expected one claim, got {len(claims)}"
    return claims[0]


# ==========================================================================
# Criterion 1 — deterministic verifier, structured verdict, no LLM/network
# ==========================================================================


def test_verifier_module_imports_no_llm_or_network_client() -> None:
    """Structural guarantee: the verifier makes no network/LLM calls."""
    from copilot import verification

    source = inspect.getsource(verification)
    tree = ast.parse(source)

    forbidden = {"httpx", "anthropic", "openai", "requests", "aiohttp", "urllib"}
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None:
                imported.add(node.module.split(".")[0])

    leaked = forbidden & imported
    assert leaked == set(), f"verifier must not import network/LLM clients: {leaked}"


def test_importing_verifier_pulls_no_network_or_llm_module() -> None:
    """Transitive guarantee (rev 3b): importing the verifier must not drag in the
    network/IO/LLM layer at all. An AST scan of one module's source sees only its
    *direct* imports; this imports the verifier in a clean subprocess and
    inspects the entire resulting module graph via ``sys.modules``.
    """
    import os
    import subprocess
    import sys

    agent_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = os.path.join(agent_root, "src")
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = src + (os.pathsep + existing if existing else "")

    code = (
        "import importlib, sys\n"
        "importlib.import_module('copilot.verification')\n"
        "forbidden = ('httpx', 'anthropic', 'openai', 'requests', 'aiohttp')\n"
        "print(','.join(sorted(m for m in forbidden if m in sys.modules)))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=agent_root,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    leaked = result.stdout.strip()
    assert leaked == "", (
        f"importing the verifier transitively pulled network/LLM modules: {leaked}"
    )


def test_verify_returns_structured_verdict() -> None:
    refs = (ref("Observation", "obs-1"),)
    verdict = verify("BP is 128 mmHg [Observation/obs-1].", refs)

    assert isinstance(verdict, contracts.VerificationVerdict)
    assert isinstance(verdict.output_text, str)
    assert isinstance(verdict.verdicts, tuple)
    assert all(isinstance(v, contracts.ClaimVerdict) for v in verdict.verdicts)
    assert isinstance(verdict.counts, contracts.VerificationCounts)


# ==========================================================================
# Criterion 2 — valid citation passes, token preserved in output
# ==========================================================================


def test_claim_with_present_citation_passes() -> None:
    refs = (ref("Observation", "obs-1"),)
    verdict = verify("The systolic BP is 128 mmHg [Observation/obs-1].", refs)

    claim = only_claim(verdict)
    assert status_value(claim) == VERIFIED
    assert verdict.counts.claims_passed == 1
    assert verdict.counts.claims_stripped == 0


def test_verified_claim_preserves_its_citation_token() -> None:
    refs = (ref("Observation", "obs-1"),)
    verdict = verify("The systolic BP is 128 mmHg [Observation/obs-1].", refs)

    assert token("Observation", "obs-1") in verdict.output_text
    assert verdict.content_removed is False
    assert verdict.fallback_triggered is False


# ==========================================================================
# Criterion 3 — cited ID not in the set is stripped and annotated;
#               available refs attached on the stripped (non-fallback) path
# ==========================================================================


def test_claim_citing_absent_id_is_stripped_while_valid_claim_survives() -> None:
    refs = (ref("Observation", "obs-1"),)
    draft = (
        "The systolic BP is 128 mmHg [Observation/obs-1]. "
        "The A1c is 8.2% [Observation/ghost-9]."
    )
    verdict = verify(draft, refs)

    assert verdict.fallback_triggered is False
    assert "8.2%" not in verdict.output_text
    assert token("Observation", "ghost-9") not in verdict.output_text
    assert token("Observation", "obs-1") in verdict.output_text

    stripped_verdict = next(
        v for v in verdict.verdicts if status_value(v) in STRIP_STATUSES
    )
    assert status_value(stripped_verdict) == "stripped_unknown_citation"


def test_stripped_claim_is_annotated_and_carries_available_refs() -> None:
    refs = (ref("Observation", "obs-1"),)
    draft = (
        "The systolic BP is 128 mmHg [Observation/obs-1]. "
        "The A1c is 8.2% [Observation/ghost-9]."
    )
    verdict = verify(draft, refs)

    assert len(verdict.stripped) == 1
    stripped = verdict.stripped[0]
    assert "8.2%" in stripped.text
    assert stripped.reason is not None and stripped.reason != ""

    assert verdict.content_removed is True
    assert verify_mod().CONTENT_REMOVED_MARKER in verdict.output_text

    # The deep-linkable refs ARCHITECTURE §5 promises, on the stripped path.
    assert set(verdict.available_refs) == set(refs)


# ==========================================================================
# Criterion 4 — no-citation clinical claim stripped; whitelist survives,
#               AND the whitelist does not over-match (the rev-2 hole)
# ==========================================================================


def _assert_stripped_as_uncited_claim(sentence: str) -> Any:
    """A bare, uncited sentence about the patient is a claim and is stripped.

    Uses empty refs so the single sentence is the whole draft: one claim, all
    stripped => full fallback. Proves the sentence was (a) counted as a claim,
    not silently kept as scaffolding, and (b) removed from the output.
    """
    verdict = verify(sentence, ())
    claim = only_claim(verdict)
    assert status_value(claim) == "stripped_uncited", (
        f"{sentence!r} should be an uncited clinical claim, "
        f"got {status_value(claim)}"
    )
    assert verdict.counts.claims_total == 1
    assert verdict.counts.claims_passed == 0
    assert verdict.counts.claims_stripped == 1
    assert verdict.fallback_triggered is True
    assert verdict.output_text == verify_mod().FALLBACK_TEXT
    return verdict


def _assert_survives_as_scaffolding(sentence: str) -> Any:
    """Process-scaffolding is kept, not counted, and never a fallback case."""
    verdict = verify(sentence, ())
    assert sentence.strip() in verdict.output_text, (
        f"{sentence!r} should survive as scaffolding"
    )
    assert verdict.counts.claims_total == 0
    assert verdict.counts.claims_stripped == 0
    assert verdict.fallback_triggered is False
    assert verdict.content_removed is False
    assert all(status_value(v) == SCAFFOLD for v in verdict.verdicts)
    return verdict


def test_uncited_clinical_claim_is_stripped_while_cited_claim_survives() -> None:
    refs = (ref("Observation", "obs-1"),)
    draft = (
        "The systolic BP is 128 mmHg [Observation/obs-1]. "
        "The patient has poorly controlled diabetes."
    )
    verdict = verify(draft, refs)

    assert verdict.fallback_triggered is False
    assert "diabetes" not in verdict.output_text
    assert verdict.content_removed is True

    stripped_verdict = next(
        v for v in verdict.verdicts if status_value(v) in STRIP_STATUSES
    )
    assert status_value(stripped_verdict) == "stripped_uncited"


# --- Mandatory adversarial STRIP block (the check whose absence sank rev 2) ---
# Each of these wears process phrasing but asserts something about the patient,
# so each must be STRIPPED as an uncited claim, never laundered as scaffolding.


def test_confirmed_allergy_is_stripped_not_scaffolding() -> None:
    _assert_stripped_as_uncited_claim("I confirmed she is allergic to penicillin.")


def test_verified_potassium_value_is_stripped_not_scaffolding() -> None:
    _assert_stripped_as_uncited_claim("I verified her potassium is 5.8 mmol/L.")


def test_confirmed_cancer_diagnosis_is_stripped_not_scaffolding() -> None:
    _assert_stripped_as_uncited_claim(
        "I confirmed the patient has stage IV pancreatic cancer."
    )


def test_no_evidence_of_malignancy_is_stripped_not_absence_coverage() -> None:
    # "no ... found" without a record-marker is a diagnostic conclusion, not an
    # absence-of-record statement.
    _assert_stripped_as_uncited_claim("No evidence of malignancy was found.")


def test_checked_that_clause_is_stripped_retrieval_verb_but_proposition() -> None:
    # Retrieval verb, but its object is a proposition about the patient, not a
    # data category — must be stripped.
    _assert_stripped_as_uncited_claim("I checked that she is hypertensive.")


# --- Extra over-match guards (beyond the mandatory list) ---


def test_confirmed_over_a_data_category_is_still_stripped() -> None:
    # "confirmed" is assertive even when its object *is* a data category; the
    # verb, not just the object, disqualifies it as coverage.
    _assert_stripped_as_uncited_claim("I confirmed the labs are abnormal.")


def test_retrieval_verb_with_trailing_clause_is_stripped() -> None:
    # A retrieval verb over a real category that then slides into a clause about
    # the patient must not be laundered by the category prefix.
    _assert_stripped_as_uncited_claim(
        "I reviewed her chart and she has metastatic disease."
    )


def test_no_finding_without_record_marker_is_stripped() -> None:
    _assert_stripped_as_uncited_claim("No signs of infection were present.")


def test_bare_clinical_assertion_is_stripped() -> None:
    _assert_stripped_as_uncited_claim("The patient is hypertensive.")


# --- Mandatory SURVIVE block: genuine scaffolding is kept uncited ---


def test_coverage_over_data_categories_survives() -> None:
    _assert_survives_as_scaffolding(
        "I checked the labs, medications, and problem list."
    )


def test_absence_of_data_category_on_record_survives() -> None:
    _assert_survives_as_scaffolding("No colonoscopy is on record for this patient.")


def test_refusal_survives() -> None:
    _assert_survives_as_scaffolding("I couldn't verify that against the record.")


def test_section_header_survives() -> None:
    _assert_survives_as_scaffolding("Current Medications:")


def test_greeting_survives() -> None:
    _assert_survives_as_scaffolding("Hello, here is what I found.")


def test_navigation_line_survives() -> None:
    _assert_survives_as_scaffolding(
        "View the source records in the chart for details."
    )


# --- Extra SURVIVE guards: whitelist admits genuine phrasings ---


def test_multiword_retrieval_verb_coverage_survives() -> None:
    _assert_survives_as_scaffolding("I looked at the allergies and problem list.")


def test_possessive_coverage_survives() -> None:
    _assert_survives_as_scaffolding("I reviewed the patient's medications.")


def test_absence_of_allergies_on_record_survives() -> None:
    _assert_survives_as_scaffolding("No allergies are on record.")


# --- Absence OBJECT constraint (rev 3b): a *finding* is not a data category ---
# The marker alone is not enough — "recorded"/"documented"/"on record" are valid
# markers, so what is absent must itself be a data category, never a finding.
# Each of these is a finding dressed as an absence line and must be STRIPPED.


def test_no_signs_of_infection_documented_is_stripped() -> None:
    _assert_stripped_as_uncited_claim("No signs of infection were documented.")


def test_no_improvement_recorded_is_stripped() -> None:
    _assert_stripped_as_uncited_claim("No improvement is recorded.")


def test_no_acute_distress_on_record_is_stripped() -> None:
    # Same marker ("on record") as the surviving colonoscopy line, so only an
    # OBJECT constraint — not a marker constraint — can strip this.
    _assert_stripped_as_uncited_claim("No acute distress is on record.")


def test_no_metastatic_disease_documented_is_stripped() -> None:
    _assert_stripped_as_uncited_claim("No metastatic disease was documented.")


# --- The data-category noun set is sourced from the tool categories ---


def test_data_category_nouns_are_sourced_from_tool_categories() -> None:
    """The whitelist's category nouns cannot drift from what the tools return.

    Every snapshot category the service actually returns must be represented in
    the verifier's single data-category constant; a new tool category with no
    representation trips ``cat in representative`` and forces this to be updated.
    """
    from copilot.tools import snapshot

    nouns = {n.lower() for n in verify_mod().DATA_CATEGORY_NOUNS}
    representative = {
        "demographics": {"demographics"},
        "medications": {"medications", "medication", "meds"},
        "problems": {"problems", "problem", "conditions"},
        "allergies": {"allergies", "allergy"},
        "labs": {"labs", "lab", "observations"},
        "last_encounter": {"encounters", "encounter"},
    }
    for cat in snapshot.SNAPSHOT_CATEGORIES:
        assert cat in representative, f"unmapped tool category: {cat}"
        assert representative[cat] & nouns, f"category {cat} not represented"


def test_scaffolding_and_claim_are_distinguished_in_one_draft() -> None:
    refs = (ref("Observation", "obs-1"),)
    draft = (
        "I checked the labs and vitals. "
        "The systolic BP is 128 mmHg [Observation/obs-1]. "
        "The patient is hypertensive."
    )
    verdict = verify(draft, refs)

    assert "I checked the labs and vitals." in verdict.output_text
    assert token("Observation", "obs-1") in verdict.output_text
    assert "hypertensive" not in verdict.output_text
    assert verdict.counts.claims_total == 2  # scaffolding excluded from the count
    assert verdict.counts.claims_passed == 1
    assert verdict.counts.claims_stripped == 1


# ==========================================================================
# Criterion 5 — no fractional threshold; fail closed only when nothing
#               claim-bearing survives; anti-dilution; no threshold knob
# ==========================================================================


def test_one_surviving_one_stripped_is_annotated_partial_not_fallback() -> None:
    refs = (ref("Observation", "obs-1"),)
    draft = (
        "The systolic BP is 128 mmHg [Observation/obs-1]. "
        "The A1c is 8.2% [Observation/ghost-9]."
    )
    verdict = verify(draft, refs)

    assert verdict.fallback_triggered is False
    assert verdict.content_removed is True
    assert verify_mod().FALLBACK_TEXT not in verdict.output_text
    assert token("Observation", "obs-1") in verdict.output_text
    assert verdict.counts.claims_total == 2
    assert verdict.counts.claims_passed == 1
    assert verdict.counts.claims_stripped == 1


def test_every_claim_stripped_triggers_full_fallback() -> None:
    refs = (ref("Observation", "obs-1"),)
    draft = (
        "The A1c is 8.2% [Observation/ghost-1]. "
        "The eGFR is 44 [Observation/ghost-2]."
    )
    verdict = verify(draft, refs)

    assert verdict.fallback_triggered is True
    assert verdict.output_text == verify_mod().FALLBACK_TEXT
    assert set(verdict.available_refs) == set(refs)
    assert "8.2%" not in verdict.output_text


def test_single_claim_draft_all_stripped_triggers_fallback() -> None:
    refs = (ref("Observation", "obs-1"),)
    verdict = verify("The A1c is 8.2% [Observation/ghost-9].", refs)

    assert verdict.fallback_triggered is True
    assert verdict.output_text == verify_mod().FALLBACK_TEXT


def test_pure_scaffolding_draft_is_not_a_fallback_case() -> None:
    draft = "Hello, here is what I found. I checked the labs and vitals."
    verdict = verify(draft, ())

    assert verdict.fallback_triggered is False
    assert verdict.content_removed is False
    assert verdict.counts.claims_total == 0
    assert verdict.counts.claims_stripped == 0
    assert "I checked the labs and vitals." in verdict.output_text
    assert verdict.output_text != verify_mod().FALLBACK_TEXT


def test_prepending_scaffolding_never_changes_the_fallback_decision() -> None:
    # Anti-dilution — the exact defect that sank rev 1.
    refs = (ref("Observation", "obs-1"),)
    base = (
        "The A1c is 8.2% [Observation/ghost-1]. "
        "The eGFR is 44 [Observation/ghost-2]."
    )
    scaffolding = "I checked the labs. I reviewed the vitals. "

    base_verdict = verify(base, refs)
    diluted_verdict = verify(scaffolding + base, refs)

    assert base_verdict.fallback_triggered is True
    assert diluted_verdict.fallback_triggered is True
    assert diluted_verdict.fallback_triggered == base_verdict.fallback_triggered
    assert diluted_verdict.output_text == verify_mod().FALLBACK_TEXT
    # The counts of claim-bearing sentences are identical — scaffolding is inert.
    assert diluted_verdict.counts.claims_total == base_verdict.counts.claims_total
    assert (
        diluted_verdict.counts.claims_stripped
        == base_verdict.counts.claims_stripped
    )


def test_no_threshold_parameter_exists() -> None:
    sig = inspect.signature(verify_mod().verify_response)
    assert "fallback_threshold" not in sig.parameters
    for name in sig.parameters:
        upper = name.upper()
        assert "THRESHOLD" not in upper
        assert "FRACTION" not in upper
        assert "FLOOR" not in upper


def test_no_threshold_constant_survives_in_the_module() -> None:
    verification = verify_mod()
    assert not hasattr(verification, "DEFAULT_FALLBACK_THRESHOLD")
    leftover = [
        name
        for name in dir(verification)
        if "THRESHOLD" in name.upper()
        or "FRACTION" in name.upper()
        or "FLOOR" in name.upper()
    ]
    assert leftover == [], f"no fractional-floor constant may remain: {leftover}"


# ==========================================================================
# Criterion 6 — type mismatch fails (set keyed by type+id, not id alone)
# ==========================================================================


def test_right_id_wrong_type_citation_fails() -> None:
    refs = (ref("Encounter", "xyz"),)
    verdict = verify("The reading was abnormal [Observation/xyz].", refs)

    claim = only_claim(verdict)
    assert status_value(claim) == "stripped_type_mismatch"
    assert "abnormal" not in verdict.output_text


def test_correct_type_for_same_id_passes() -> None:
    refs = (ref("Encounter", "xyz"),)
    verdict = verify("The encounter was for chest pain [Encounter/xyz].", refs)

    claim = only_claim(verdict)
    assert status_value(claim) == VERIFIED
    assert token("Encounter", "xyz") in verdict.output_text


# ==========================================================================
# Criterion 7 — verdict counts for observability, even under fallback
# ==========================================================================


def test_verdict_counts_total_passed_stripped_on_partial() -> None:
    refs = (ref("Observation", "obs-1"), ref("Condition", "cond-1"))
    draft = (
        "I reviewed the record. "  # scaffolding — not counted
        "BP is 128 mmHg [Observation/obs-1]. "  # verified
        "The patient has hypertension [Condition/cond-1]. "  # verified
        "The A1c is rising [Observation/ghost]. "  # stripped: unknown id
        "The patient is diabetic."  # stripped: uncited
    )
    verdict = verify(draft, refs)

    counts = verdict.counts
    assert counts.claims_total == 4
    assert counts.claims_passed == 2
    assert counts.claims_stripped == 2
    assert counts.claims_passed + counts.claims_stripped == counts.claims_total
    assert verdict.fallback_triggered is False


def test_counts_are_reported_even_when_fallback_triggers() -> None:
    refs: tuple[Any, ...] = ()
    draft = "The A1c is rising [Observation/ghost]. The patient is diabetic."
    verdict = verify(draft, refs)

    assert verdict.fallback_triggered is True
    assert verdict.output_text == verify_mod().FALLBACK_TEXT
    counts = verdict.counts
    assert counts.claims_total == 2
    assert counts.claims_passed == 0
    assert counts.claims_stripped == 2
