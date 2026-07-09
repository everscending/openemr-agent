"""Verification layer — citation grounding, fail closed (T008).

ARCHITECTURE.md §5 Layer 1: every clinical claim in model output must cite
``[ResourceType/id]``; a deterministic, non-LLM post-processor verifies every
cited ID appeared in this request's tool results. Uncited or falsely-cited
claims fail closed.

Criteria map:
  1. Deterministic verifier over (draft text, set of ``ResourceRef``s) returns
     a structured verdict (verified text + per-claim outcomes). No LLM/network
     imports — asserted structurally by parsing the module source.
  2. A claim citing an ID in the tool-result set passes; its token is
     preserved in the output for panel-side rendering.
  3. A claim citing an ID NOT in the set is stripped and annotated
     (machine-readable stripped list + user-visible removal marker).
  4. A sentence making a clinical claim with NO citation is stripped and
     annotated; whitelisted scaffolding (coverage/refusal/navigation) survives.
  5. Fail-closed floor: strip more than a configurable fraction of
     claim-bearing sentences (default >= 50%) => whole response replaced by the
     fallback, carrying the available ``ResourceRef``s. Asserted at the
     boundary (just under => partial; at/over => full fallback).
  6. Type mismatch fails: citing ``[Observation/xyz]`` when ``xyz`` was
     returned as an Encounter is a failed citation (set keyed by type+id).
  7. Verdicts carry counts (total / passed / stripped) for observability.

Production code is imported lazily inside test bodies so collection succeeds
before the implementation exists (RED = the missing feature per test).
"""

from __future__ import annotations

import ast
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


# ==========================================================================
# Criterion 1 — deterministic verifier, structured verdict, no LLM/network
# ==========================================================================


def test_verifier_module_imports_no_llm_or_network_client() -> None:
    """Structural guarantee: the verifier makes no network/LLM calls."""
    import inspect

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


def test_verify_returns_structured_verdict() -> None:
    refs = (ref("Observation", "obs-1"),)
    verdict = verify("BP is 128 mmHg [Observation/obs-1].", refs)

    assert isinstance(verdict, contracts.VerificationVerdict)
    assert isinstance(verdict.output_text, str)
    assert isinstance(verdict.verdicts, tuple)
    # Per-claim outcomes are present and typed.
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

    # Token survives verification for panel-side rendering.
    assert token("Observation", "obs-1") in verdict.output_text
    assert verdict.content_removed is False
    assert verdict.fallback_triggered is False


# ==========================================================================
# Criterion 3 — cited ID not in the set is stripped and annotated
# ==========================================================================


def test_claim_citing_absent_id_is_stripped() -> None:
    refs = (ref("Observation", "obs-1"),)
    verdict = verify("The A1c is 8.2% [Observation/ghost-9].", refs)

    claim = only_claim(verdict)
    assert status_value(claim) in STRIP_STATUSES
    # The falsely-cited sentence and its token are gone from the output.
    assert "8.2%" not in verdict.output_text
    assert token("Observation", "ghost-9") not in verdict.output_text


def test_stripped_claim_is_annotated_machine_readable_and_user_visible() -> None:
    refs = (ref("Observation", "obs-1"),)
    verdict = verify("The A1c is 8.2% [Observation/ghost-9].", refs)

    # Machine-readable list of stripped claims with reasons.
    assert len(verdict.stripped) == 1
    stripped = verdict.stripped[0]
    assert "8.2%" in stripped.text
    assert stripped.reason is not None and stripped.reason != ""
    # User-visible marker that content was removed.
    assert verdict.content_removed is True
    assert verify_mod().CONTENT_REMOVED_MARKER in verdict.output_text


# ==========================================================================
# Criterion 4 — no-citation clinical claim stripped; whitelist survives
# ==========================================================================


def test_uncited_clinical_claim_is_stripped() -> None:
    # A bare clinical assertion with no citation token and no scaffolding
    # phrasing must not be stated as fact.
    verdict = verify("The patient has poorly controlled diabetes.", ())

    claim = only_claim(verdict)
    assert status_value(claim) == "stripped_uncited"
    assert "diabetes" not in verdict.output_text
    assert verdict.content_removed is True


@pytest.mark.parametrize(
    "sentence",
    [
        "Hello, here is what I found.",  # greeting
        "I checked the labs, medications, and problem list.",  # coverage
        "No colonoscopy is on record for this patient.",  # coverage / absence
        "I couldn't verify that against the record.",  # refusal
        "View the source records in the chart for details.",  # navigation
        "Current Medications:",  # section header
    ],
)
def test_whitelisted_scaffolding_survives_without_a_citation(sentence: str) -> None:
    verdict = verify(sentence, ())

    # Scaffolding is not a clinical claim, so it is kept and not counted.
    assert sentence.strip() in verdict.output_text
    assert verdict.counts.claims_total == 0
    assert all(status_value(v) == SCAFFOLD for v in verdict.verdicts)


def test_scaffolding_and_claim_are_distinguished_in_one_draft() -> None:
    refs = (ref("Observation", "obs-1"),)
    draft = (
        "I checked the labs and vitals. "
        "The systolic BP is 128 mmHg [Observation/obs-1]. "
        "The patient is hypertensive."
    )
    verdict = verify(draft, refs)

    # Coverage line kept, cited claim kept, uncited clinical claim stripped.
    assert "I checked the labs and vitals." in verdict.output_text
    assert token("Observation", "obs-1") in verdict.output_text
    assert "hypertensive" not in verdict.output_text
    assert verdict.counts.claims_total == 2  # scaffolding excluded
    assert verdict.counts.claims_passed == 1
    assert verdict.counts.claims_stripped == 1


# ==========================================================================
# Criterion 5 — fail-closed floor at the boundary; configurable; carries refs
# ==========================================================================


def _four_claims_draft(n_bad: int) -> tuple[str, tuple[Any, ...]]:
    """Draft with 4 cited clinical claims, ``n_bad`` of them falsely cited."""
    good_refs = tuple(ref("Observation", f"obs-{i}") for i in range(4))
    sentences = []
    for i in range(4):
        if i < n_bad:
            sentences.append(f"Finding {i} is notable [Observation/ghost-{i}].")
        else:
            sentences.append(f"Finding {i} is notable [Observation/obs-{i}].")
    return " ".join(sentences), good_refs


def test_stripping_just_under_threshold_yields_annotated_partial() -> None:
    # 1 of 4 claims stripped => 0.25 < 0.5 default => partial output, not fallback.
    draft, refs = _four_claims_draft(n_bad=1)
    verdict = verify(draft, refs)

    assert verdict.fallback_triggered is False
    assert verdict.content_removed is True
    assert verdict.counts.claims_total == 4
    assert verdict.counts.claims_stripped == 1
    # Surviving cited claims remain in the output.
    assert token("Observation", "obs-3") in verdict.output_text
    assert verify_mod().FALLBACK_TEXT not in verdict.output_text


def test_stripping_at_threshold_triggers_full_fallback() -> None:
    # 2 of 4 claims stripped => 0.5 >= 0.5 default => full fallback replacement.
    draft, refs = _four_claims_draft(n_bad=2)
    verdict = verify(draft, refs)

    assert verdict.fallback_triggered is True
    assert verdict.output_text == verify_mod().FALLBACK_TEXT
    # None of the original claims survive in the replaced output.
    assert token("Observation", "obs-3") not in verdict.output_text


def test_fallback_threshold_is_configurable() -> None:
    draft, refs = _four_claims_draft(n_bad=1)  # 0.25 stripped fraction

    # A stricter threshold flips the same 0.25 case into full fallback.
    strict = verify(draft, refs, fallback_threshold=0.2)
    assert strict.fallback_triggered is True

    # A looser threshold keeps a heavily-stripped case as partial.
    draft2, refs2 = _four_claims_draft(n_bad=3)  # 0.75 stripped fraction
    loose = verify(draft2, refs2, fallback_threshold=0.9)
    assert loose.fallback_triggered is False


def test_fallback_carries_available_refs() -> None:
    draft, refs = _four_claims_draft(n_bad=2)
    verdict = verify(draft, refs)

    assert verdict.fallback_triggered is True
    # The fallback surfaces the refs that were available this request.
    assert set(verdict.available_refs) == set(refs)


def test_default_fallback_threshold_is_one_half() -> None:
    assert verify_mod().DEFAULT_FALLBACK_THRESHOLD == 0.5


# ==========================================================================
# Criterion 6 — type mismatch fails (set keyed by type+id, not id alone)
# ==========================================================================


def test_right_id_wrong_type_citation_fails() -> None:
    # xyz was returned as an Encounter; citing it as an Observation must fail.
    refs = (ref("Encounter", "xyz"),)
    verdict = verify("The reading was abnormal [Observation/xyz].", refs)

    claim = only_claim(verdict)
    assert status_value(claim) == "stripped_type_mismatch"
    assert "abnormal" not in verdict.output_text


def test_correct_type_for_same_id_passes() -> None:
    # Control: the very same id cited under its true type verifies.
    refs = (ref("Encounter", "xyz"),)
    verdict = verify("The encounter was for chest pain [Encounter/xyz].", refs)

    claim = only_claim(verdict)
    assert status_value(claim) == VERIFIED
    assert token("Encounter", "xyz") in verdict.output_text


# ==========================================================================
# Criterion 7 — verdict counts for observability
# ==========================================================================


def test_verdict_counts_total_passed_stripped() -> None:
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


# ---------------------------------------------------------------------------
# Small helper used across criteria
# ---------------------------------------------------------------------------


def only_claim(verdict: Any) -> Any:
    """The single claim-bearing verdict (excludes scaffolding)."""
    claims = [v for v in verdict.verdicts if status_value(v) != SCAFFOLD]
    assert len(claims) == 1, f"expected one claim, got {len(claims)}"
    return claims[0]
