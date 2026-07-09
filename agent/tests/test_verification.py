"""Verification layer — citation grounding, fail closed (T008, rev 2).

ARCHITECTURE.md §5 Layer 1: every clinical claim in model output must cite
``[ResourceType/id]``; a deterministic, non-LLM post-processor verifies every
cited ID appeared in this request's tool results (keyed by **type + id**).
Uncited or falsely-cited claims fail closed.

Corrected fail-closed rule (rev 2 — no fractional floor): if at least one
claim-bearing sentence survives, the survivors are emitted with the removal
annotation. Only when **no** claim-bearing sentence survives *and* at least
one claim was stripped is the whole response replaced by the fallback text.
A draft that made no claims at all (pure scaffolding) is never a fallback
case. There is no threshold parameter, no configurable fraction, and no
``DEFAULT_FALLBACK_THRESHOLD``-style constant. Adding scaffolding to a draft
must never change the fallback decision (the bug that sank rev 1).

Criteria map:
  1. Deterministic verifier over (draft text, set of ``ResourceRef``s) returns
     a structured verdict (verified text + per-claim outcomes + counts). No
     LLM/network imports — asserted structurally by parsing the module source.
  2. A claim citing an ID in the tool-result set passes; its token is
     preserved in the output for panel-side rendering.
  3. A claim citing an ID NOT in the set is stripped and annotated
     (machine-readable stripped list + user-visible removal marker), and the
     available ``ResourceRef``s are attached on the stripped (non-fallback)
     path.
  4. A sentence making a clinical claim with NO citation is stripped and
     annotated; whitelisted scaffolding (greeting/coverage/refusal/navigation/
     header) survives without a citation.
  5. No fractional threshold. one-survives-one-stripped => annotated partial,
     no fallback; every claim stripped (incl. a single-claim draft) => full
     fallback with ``output_text`` exactly the fallback text; pure scaffolding
     => no fallback, nothing stripped. Anti-dilution: prepending scaffolding to
     an all-stripped draft never changes the fallback decision. No threshold
     parameter/constant exists.
  6. Type mismatch fails: citing ``[Observation/xyz]`` when ``xyz`` was
     returned as an Encounter is a failed citation (set keyed by type+id).
  7. Verdict counts (total / passed / stripped) are present and correct — even
     when the fallback triggers (observability needs to see what was stripped).

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
# Criterion 3 — cited ID not in the set is stripped and annotated;
#               available refs attached on the stripped (non-fallback) path
# ==========================================================================


def test_claim_citing_absent_id_is_stripped_while_valid_claim_survives() -> None:
    # One good + one falsely-cited claim => partial (not fallback), so we can
    # observe the stripped path directly rather than the fallback replacement.
    refs = (ref("Observation", "obs-1"),)
    draft = (
        "The systolic BP is 128 mmHg [Observation/obs-1]. "
        "The A1c is 8.2% [Observation/ghost-9]."
    )
    verdict = verify(draft, refs)

    assert verdict.fallback_triggered is False
    # The falsely-cited sentence and its token are gone from the output.
    assert "8.2%" not in verdict.output_text
    assert token("Observation", "ghost-9") not in verdict.output_text
    # The genuine claim and its token survive.
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

    # Machine-readable list of stripped claims with reasons.
    assert len(verdict.stripped) == 1
    stripped = verdict.stripped[0]
    assert "8.2%" in stripped.text
    assert stripped.reason is not None and stripped.reason != ""

    # User-visible marker that content was removed.
    assert verdict.content_removed is True
    assert verify_mod().CONTENT_REMOVED_MARKER in verdict.output_text

    # The deep-linkable refs ARCHITECTURE §5 promises, on the stripped path.
    assert set(verdict.available_refs) == set(refs)


# ==========================================================================
# Criterion 4 — no-citation clinical claim stripped; whitelist survives
# ==========================================================================


def test_uncited_clinical_claim_is_stripped_while_cited_claim_survives() -> None:
    # Direction one: a bare clinical assertion with no citation and no
    # scaffolding phrasing must not be stated as fact. Paired with a surviving
    # cited claim so this is the partial (non-fallback) path.
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
    # Direction two: our own template scaffolding is not a clinical claim, so
    # it is kept, not counted, and not treated as an uncited claim.
    verdict = verify(sentence, ())

    assert sentence.strip() in verdict.output_text
    assert verdict.counts.claims_total == 0
    assert verdict.fallback_triggered is False
    assert verdict.content_removed is False
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
    # Surviving cited claim remains in the output.
    assert token("Observation", "obs-1") in verdict.output_text
    assert verdict.counts.claims_total == 2
    assert verdict.counts.claims_passed == 1
    assert verdict.counts.claims_stripped == 1


def test_every_claim_stripped_triggers_full_fallback() -> None:
    # Multiple claims, all falsely cited => nothing claim-bearing survives.
    refs = (ref("Observation", "obs-1"),)
    draft = (
        "The A1c is 8.2% [Observation/ghost-1]. "
        "The eGFR is 44 [Observation/ghost-2]."
    )
    verdict = verify(draft, refs)

    assert verdict.fallback_triggered is True
    assert verdict.output_text == verify_mod().FALLBACK_TEXT
    assert set(verdict.available_refs) == set(refs)
    # None of the original content survives.
    assert "8.2%" not in verdict.output_text


def test_single_claim_draft_all_stripped_triggers_fallback() -> None:
    # The boundary rev 1 mishandled: a one-claim draft whose only claim is
    # stripped has no survivor => full fallback, exact fallback text.
    refs = (ref("Observation", "obs-1"),)
    verdict = verify("The A1c is 8.2% [Observation/ghost-9].", refs)

    assert verdict.fallback_triggered is True
    assert verdict.output_text == verify_mod().FALLBACK_TEXT


def test_pure_scaffolding_draft_is_not_a_fallback_case() -> None:
    # No claim was ever made, so nothing is wrong and nothing is stripped.
    draft = "Hello, here is what I found. I checked the labs and vitals."
    verdict = verify(draft, ())

    assert verdict.fallback_triggered is False
    assert verdict.content_removed is False
    assert verdict.counts.claims_total == 0
    assert verdict.counts.claims_stripped == 0
    assert "I checked the labs and vitals." in verdict.output_text
    assert verdict.output_text != verify_mod().FALLBACK_TEXT


def test_prepending_scaffolding_never_changes_the_fallback_decision() -> None:
    # Anti-dilution — the exact defect that sank rev 1. An all-stripped draft
    # must trigger fallback; padding it with non-claim scaffolding sentences
    # must not disarm the fallback.
    refs = (ref("Observation", "obs-1"),)
    base = (
        "The A1c is 8.2% [Observation/ghost-1]. "
        "The eGFR is 44 [Observation/ghost-2]."
    )
    scaffolding = "I checked the labs. I reviewed the vitals. "

    base_verdict = verify(base, refs)
    diluted_verdict = verify(scaffolding + base, refs)

    # Adding scaffolding changes neither the decision nor the emitted text.
    assert base_verdict.fallback_triggered is True
    assert diluted_verdict.fallback_triggered is True
    assert diluted_verdict.fallback_triggered == base_verdict.fallback_triggered
    assert diluted_verdict.output_text == verify_mod().FALLBACK_TEXT


def test_no_threshold_parameter_exists() -> None:
    sig = inspect.signature(verify_mod().verify_response)
    assert "fallback_threshold" not in sig.parameters
    # No parameter that is a threshold/fraction/floor knob under any name.
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
    # Both survivors emitted; both failures removed.
    assert verdict.fallback_triggered is False


def test_counts_are_reported_even_when_fallback_triggers() -> None:
    # Observability must see what was stripped even though the whole response
    # was replaced by the fallback text.
    refs: tuple[Any, ...] = ()
    draft = "The A1c is rising [Observation/ghost]. The patient is diabetic."
    verdict = verify(draft, refs)

    assert verdict.fallback_triggered is True
    assert verdict.output_text == verify_mod().FALLBACK_TEXT
    counts = verdict.counts
    assert counts.claims_total == 2
    assert counts.claims_passed == 0
    assert counts.claims_stripped == 2
