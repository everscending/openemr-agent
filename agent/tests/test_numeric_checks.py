"""Tests for numeric and date claim checks (T009, Layer-1 content check).

ARCHITECTURE.md §5: "where a claim contains parseable numerics (value/unit/date),
they are compared against the cited resource's actual fields." T008 grounds a
claim's *citation*; T009 checks the claim's *content* against the resource that
citation points at — a grounded citation with a fabricated value is the exact
failure T008 cannot catch alone.

Design decisions being pinned here (from the rev-2 ticket, not relitigated):
  * No tolerance. ``decimal.Decimal``, exact after trailing-zero normalization:
    ``8.20`` == ``8.2``; ``8.19`` != ``8.2``.
  * ``unchecked`` is a pass-through, so it is used *only* when no comparison is
    possible. The value comparison always runs when both sides have a parseable
    number, even if units are absent/unknown.
  * Unit rules: convertible-and-known → convert then compare; known-but-not-
    convertible → mismatch/strip; either absent/unknown → compare values anyway.
    Table is minimal (mg/g, mcg/mg; %, mmol/L, mL only to themselves/dimension).
  * Two new stripped statuses: ``STRIPPED_NUMERIC_MISMATCH`` /
    ``STRIPPED_DATE_MISMATCH`` — never reuse ``STRIPPED_UNKNOWN_CITATION``.
  * Backward compatible: ``verify_response(draft, refs, *, resources=None)``.
    With ``resources=None`` every claim is ``numeric: unchecked`` and behavior
    is identical to T008 (whose 43 locked tests must stay green).
  * Multiple citations: a quantity passes if it matches *at least one* cited
    comparable resource; strips if ≥1 is comparable and none match; unchecked
    only if no cited resource has a comparable field.

Production code is imported lazily inside test bodies so collection succeeds
before the implementation exists (RED = the missing feature, per test).
"""

from __future__ import annotations

import ast
import inspect
from decimal import Decimal
from typing import Any

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


def facts(
    resource_type: str,
    resource_id: str,
    *,
    value: Any = None,
    unit: str | None = None,
    date: str | None = None,
) -> Any:
    """Build a ``ResourceFacts`` — the cited resource's structured fields."""
    return contracts.ResourceFacts(
        ref=ref(resource_type, resource_id),
        value=value,
        unit=unit,
        date=date,
    )


def token(resource_type: str, resource_id: str) -> str:
    return f"[{resource_type}/{resource_id}]"


VERIFIED = "verified"
NUM_MISMATCH = "stripped_numeric_mismatch"
DATE_MISMATCH = "stripped_date_mismatch"
CHECKED = "checked"
UNCHECKED = "unchecked"


def status_value(obj: Any) -> str:
    """A status/enum as its plain string value (enum-agnostic)."""
    status = getattr(obj, "status", obj)
    return getattr(status, "value", status)


def numeric_value(claim: Any) -> str | None:
    """The per-claim numeric-check outcome as a plain string, or None."""
    numeric = getattr(claim, "numeric", None)
    if numeric is None:
        return None
    check = getattr(numeric, "check", None)
    return getattr(check, "value", check)


def only_claim(verdict: Any) -> Any:
    """The single claim-bearing verdict (excludes scaffolding)."""
    claims = [
        v for v in verdict.verdicts if status_value(v) != "scaffolding"
    ]
    assert len(claims) == 1, f"expected one claim, got {len(claims)}"
    return claims[0]


# ==========================================================================
# Criterion 1 — deterministic, pure, no LLM/network (direct + transitive)
# ==========================================================================


def test_checker_module_imports_no_llm_or_network_client() -> None:
    """Direct AST scan: the module names no network/LLM client import."""
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
    assert leaked == set(), f"checker must not import network/LLM clients: {leaked}"


def test_importing_checker_pulls_no_network_or_llm_module() -> None:
    """Transitive guarantee: importing the module in a *clean subprocess* must not
    drag the network/IO/LLM layer into ``sys.modules``. An AST scan of one
    module's source sees only its direct imports and is blind to transitive
    coupling — that mistake shipped once already, so this inspects the whole
    resulting module graph.
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
        f"importing the checker transitively pulled network/LLM modules: {leaked}"
    )


def test_checker_is_pure_and_deterministic() -> None:
    """Same inputs → identical verdict, twice (no I/O, no clock, no state)."""
    refs = (ref("Observation", "obs-1"),)
    res = (facts("Observation", "obs-1", value=Decimal("8.2"), unit="%"),)
    draft = "A1c is 8.2% [Observation/obs-1]."

    first = verify(draft, refs, resources=res)
    second = verify(draft, refs, resources=res)
    assert first.output_text == second.output_text
    assert first.counts == second.counts
    assert first.verdicts == second.verdicts


# ==========================================================================
# Criterion 2 — matching numeric passes and stays in the output
# ==========================================================================


def test_matching_numeric_passes_and_survives() -> None:
    refs = (ref("Observation", "obs-1"),)
    res = (facts("Observation", "obs-1", value=Decimal("8.2"), unit="%"),)
    verdict = verify("A1c is 8.2% [Observation/obs-1].", refs, resources=res)

    claim = only_claim(verdict)
    assert status_value(claim) == VERIFIED
    assert numeric_value(claim) == CHECKED
    assert "8.2%" in verdict.output_text
    assert token("Observation", "obs-1") in verdict.output_text
    assert verdict.content_removed is False
    assert verdict.fallback_triggered is False
    assert verdict.counts.numeric_checked == 1


def test_trailing_zero_8_20_matches_8_2() -> None:
    refs = (ref("Observation", "obs-1"),)
    res = (facts("Observation", "obs-1", value=Decimal("8.2"), unit="%"),)
    verdict = verify("A1c is 8.20% [Observation/obs-1].", refs, resources=res)

    claim = only_claim(verdict)
    assert status_value(claim) == VERIFIED
    assert numeric_value(claim) == CHECKED
    assert verdict.content_removed is False


# ==========================================================================
# Criterion 3 — contradicting numeric is stripped, annotated, flows through
#               T008's strip/annotate path
# ==========================================================================


def test_contradicting_numeric_is_stripped_numeric_mismatch() -> None:
    refs = (ref("Observation", "obs-1"), ref("Observation", "obs-2"))
    res = (
        facts("Observation", "obs-1", value=Decimal("128"), unit="mmHg"),
        facts("Observation", "obs-2", value=Decimal("8.2"), unit="%"),
    )
    draft = (
        "The systolic BP is 128 mmHg [Observation/obs-1]. "
        "The A1c is 7.1% [Observation/obs-2]."
    )
    verdict = verify(draft, refs, resources=res)

    assert verdict.fallback_triggered is False
    assert "7.1%" not in verdict.output_text
    assert token("Observation", "obs-1") in verdict.output_text

    stripped_verdict = next(
        v for v in verdict.verdicts if status_value(v) == NUM_MISMATCH
    )
    assert status_value(stripped_verdict) == NUM_MISMATCH

    # Distinct status: a grounded citation with a bad value is NOT an unknown
    # citation.
    assert status_value(stripped_verdict) != "stripped_unknown_citation"


def test_numeric_mismatch_flows_through_strip_annotate_path() -> None:
    refs = (ref("Observation", "obs-1"), ref("Observation", "obs-2"))
    res = (
        facts("Observation", "obs-1", value=Decimal("128"), unit="mmHg"),
        facts("Observation", "obs-2", value=Decimal("8.2"), unit="%"),
    )
    draft = (
        "The systolic BP is 128 mmHg [Observation/obs-1]. "
        "The A1c is 7.1% [Observation/obs-2]."
    )
    verdict = verify(draft, refs, resources=res)

    assert verdict.content_removed is True
    assert verify_mod().CONTENT_REMOVED_MARKER in verdict.output_text
    assert set(verdict.available_refs) == set(refs)

    assert len(verdict.stripped) == 1
    stripped = verdict.stripped[0]
    assert status_value(stripped) == NUM_MISMATCH
    # Machine-readable reason names claimed vs actual value.
    assert "7.1" in stripped.reason
    assert "8.2" in stripped.reason


def test_8_19_vs_8_2_strips_no_tolerance() -> None:
    refs = (ref("Observation", "obs-1"),)
    res = (facts("Observation", "obs-1", value=Decimal("8.2"), unit="%"),)
    verdict = verify("A1c is 8.19% [Observation/obs-1].", refs, resources=res)

    claim = only_claim(verdict)
    assert status_value(claim) == NUM_MISMATCH


# ==========================================================================
# Criterion 4 — units, all branches
# ==========================================================================


def test_unit_same_known_passes() -> None:
    refs = (ref("Observation", "obs-1"),)
    res = (facts("Observation", "obs-1", value=Decimal("150"), unit="mg"),)
    verdict = verify("Dose is 150 mg [Observation/obs-1].", refs, resources=res)

    claim = only_claim(verdict)
    assert status_value(claim) == VERIFIED
    assert numeric_value(claim) == CHECKED


def test_unit_known_nonconvertible_strips() -> None:
    # mg vs mL: both known, not convertible → mismatch, strip (NOT a value pass).
    refs = (ref("Observation", "obs-1"),)
    res = (facts("Observation", "obs-1", value=Decimal("150"), unit="mL"),)
    verdict = verify("Dose is 150 mg [Observation/obs-1].", refs, resources=res)

    claim = only_claim(verdict)
    assert status_value(claim) == NUM_MISMATCH


def test_unit_convertible_g_to_mg_passes() -> None:
    refs = (ref("Observation", "obs-1"),)
    res = (facts("Observation", "obs-1", value=Decimal("150"), unit="mg"),)
    verdict = verify("Dose is 0.15 g [Observation/obs-1].", refs, resources=res)

    claim = only_claim(verdict)
    assert status_value(claim) == VERIFIED
    assert numeric_value(claim) == CHECKED


def test_no_resource_unit_passes_on_value_unit_not_checked() -> None:
    refs = (ref("Observation", "obs-1"),)
    res = (facts("Observation", "obs-1", value=Decimal("150")),)  # no unit
    verdict = verify("Dose is 150 mg [Observation/obs-1].", refs, resources=res)

    claim = only_claim(verdict)
    assert status_value(claim) == VERIFIED
    assert numeric_value(claim) == CHECKED
    # unit was not checkable (resource carried no unit).
    assert claim.numeric.unit_checked is False


def test_unknown_unit_passes_on_value_records_unit_unchecked() -> None:
    refs = (ref("Observation", "obs-1"),)
    res = (facts("Observation", "obs-1", value=Decimal("150"), unit="mg"),)
    verdict = verify(
        "Dose is 150 widgets [Observation/obs-1].", refs, resources=res
    )

    claim = only_claim(verdict)
    assert status_value(claim) == VERIFIED
    assert numeric_value(claim) == CHECKED
    assert claim.numeric.unit_checked is False


# ==========================================================================
# Criterion 5 — dates, day precision, and the data-quality boundary
# ==========================================================================


def test_matching_date_passes() -> None:
    refs = (ref("Encounter", "enc-1"),)
    res = (facts("Encounter", "enc-1", date="2024-03-15"),)
    verdict = verify(
        "The visit was on 2024-03-15 [Encounter/enc-1].", refs, resources=res
    )

    claim = only_claim(verdict)
    assert status_value(claim) == VERIFIED
    assert numeric_value(claim) == CHECKED


def test_contradicting_date_strips_date_mismatch() -> None:
    refs = (ref("Encounter", "enc-1"),)
    res = (facts("Encounter", "enc-1", date="2024-03-15"),)
    verdict = verify(
        "The visit was on 2024-03-16 [Encounter/enc-1].", refs, resources=res
    )

    claim = only_claim(verdict)
    assert status_value(claim) == DATE_MISMATCH
    stripped = verdict.stripped[0]
    assert status_value(stripped) == DATE_MISMATCH


def test_zero_date_makes_date_claim_unchecked_no_exception_no_parsed_date() -> None:
    # §8 data-quality boundary: 0000-00-00 renders as "unknown", never date math.
    refs = (ref("Encounter", "enc-1"),)
    res = (facts("Encounter", "enc-1", date="0000-00-00"),)
    verdict = verify(
        "The visit was on 2024-03-15 [Encounter/enc-1].", refs, resources=res
    )

    claim = only_claim(verdict)
    assert status_value(claim) == VERIFIED  # survives — unchecked is pass-through
    assert numeric_value(claim) == UNCHECKED
    assert claim.numeric.resource_date_unknown is True


def test_resource_date_parser_never_yields_a_date_for_zero_date() -> None:
    """The parser must return "unknown" (never a real date) for 0000-00-00,
    empty, and unparseable — and must never raise."""
    import datetime

    parse = verify_mod()._parse_resource_date
    for bad in ("0000-00-00", "0000-00-00T00:00:00Z", "", "   ", None, "not-a-date"):
        result = parse(bad)
        assert result is None, f"{bad!r} should be unknown, got {result!r}"
    # A genuine date still parses.
    good = parse("2024-03-15")
    assert isinstance(good, datetime.date)
    assert good == datetime.date(2024, 3, 15)


def test_month_year_date_matches_at_month_precision() -> None:
    refs = (ref("Encounter", "enc-1"),)
    res = (facts("Encounter", "enc-1", date="2025-01-20"),)
    verdict = verify(
        "Lisinopril was started January 2025 [Encounter/enc-1].",
        refs,
        resources=res,
    )

    claim = only_claim(verdict)
    assert status_value(claim) == VERIFIED
    assert numeric_value(claim) == CHECKED


# ==========================================================================
# Criterion 6 — no parseable numeric/date → unchecked, survives, visible in
#               counts, and NOT reported as numerically verified anywhere
# ==========================================================================


def test_no_numeric_claim_is_unchecked_and_survives() -> None:
    refs = (ref("Condition", "cond-1"),)
    res = (facts("Condition", "cond-1"),)  # nothing numeric to compare
    verdict = verify(
        "The patient has hypertension [Condition/cond-1].", refs, resources=res
    )

    claim = only_claim(verdict)
    assert status_value(claim) == VERIFIED
    assert numeric_value(claim) == UNCHECKED
    assert "hypertension" in verdict.output_text
    assert verdict.counts.numeric_unchecked == 1
    assert verdict.counts.numeric_checked == 0


def test_unchecked_is_not_reported_as_numerically_verified_anywhere() -> None:
    refs = (ref("Condition", "cond-1"),)
    res = (facts("Condition", "cond-1"),)
    verdict = verify(
        "The patient has hypertension [Condition/cond-1].", refs, resources=res
    )

    # It grounding-passed, but must not be counted as numerically checked.
    assert verdict.counts.numeric_checked == 0
    unchecked_claims = [
        v for v in verdict.verdicts if numeric_value(v) == UNCHECKED
    ]
    assert len(unchecked_claims) == 1
    for v in verdict.verdicts:
        if numeric_value(v) == UNCHECKED:
            assert numeric_value(v) != CHECKED


def test_counts_split_checked_and_unchecked() -> None:
    refs = (ref("Observation", "obs-1"), ref("Condition", "cond-1"))
    res = (
        facts("Observation", "obs-1", value=Decimal("8.2"), unit="%"),
        facts("Condition", "cond-1"),
    )
    draft = (
        "A1c is 8.2% [Observation/obs-1]. "
        "The patient has hypertension [Condition/cond-1]."
    )
    verdict = verify(draft, refs, resources=res)

    assert verdict.counts.numeric_checked == 1
    assert verdict.counts.numeric_unchecked == 1
    assert verdict.counts.claims_passed == 2
    assert verdict.counts.claims_stripped == 0


# ==========================================================================
# Criterion 7 — additive; resources=None is backward compatible
# ==========================================================================


def test_resources_none_marks_every_claim_unchecked() -> None:
    refs = (ref("Observation", "obs-1"),)
    verdict = verify("A1c is 8.2% [Observation/obs-1].", refs)  # no resources

    claim = only_claim(verdict)
    assert status_value(claim) == VERIFIED
    assert numeric_value(claim) == UNCHECKED
    assert verdict.counts.numeric_checked == 0
    assert verdict.counts.numeric_unchecked == 1


def test_resources_none_is_behaviorally_identical_to_omitting_them() -> None:
    refs = (ref("Observation", "obs-1"),)
    draft = "A1c is 8.2% [Observation/obs-1]."

    without = verify(draft, refs)
    explicit_none = verify(draft, refs, resources=None)

    assert without.output_text == explicit_none.output_text
    assert without.counts == explicit_none.counts
    assert without.fallback_triggered == explicit_none.fallback_triggered


def test_resources_none_does_not_strip_a_value_that_would_mismatch() -> None:
    # Guard: without resources the content check must NOT run — a value that
    # would contradict a resource still survives (byte-identical to T008).
    refs = (ref("Observation", "obs-1"),)
    verdict = verify("A1c is 999 [Observation/obs-1].", refs)

    claim = only_claim(verdict)
    assert status_value(claim) == VERIFIED
    assert verdict.content_removed is False
    assert verdict.fallback_triggered is False


def test_additive_contract_fields_exist() -> None:
    counts = contracts.VerificationCounts(
        claims_total=0, claims_passed=0, claims_stripped=0
    )
    assert counts.numeric_checked == 0
    assert counts.numeric_unchecked == 0

    verdict = contracts.ClaimVerdict(status="verified", text="x")
    assert verdict.numeric is None  # default: not evaluated

    # The two new stripped statuses exist and are distinct.
    assert contracts.ClaimStatus.STRIPPED_NUMERIC_MISMATCH.value == NUM_MISMATCH
    assert contracts.ClaimStatus.STRIPPED_DATE_MISMATCH.value == DATE_MISMATCH
    assert (
        contracts.ClaimStatus.STRIPPED_NUMERIC_MISMATCH
        != contracts.ClaimStatus.STRIPPED_DATE_MISMATCH
    )
    assert (
        contracts.ClaimStatus.STRIPPED_NUMERIC_MISMATCH
        != contracts.ClaimStatus.STRIPPED_UNKNOWN_CITATION
    )


# ==========================================================================
# Mandatory adversarial probes (each pinned as a named test)
# ==========================================================================


def test_probe_150mg_vs_150ml_strips() -> None:
    refs = (ref("Observation", "obs-1"),)
    res = (facts("Observation", "obs-1", value=Decimal("150"), unit="mL"),)
    verdict = verify("Dose is 150 mg [Observation/obs-1].", refs, resources=res)

    assert status_value(only_claim(verdict)) == NUM_MISMATCH


def test_probe_a1c_8_2_no_unit_vs_percent_passes() -> None:
    refs = (ref("Observation", "obs-1"),)
    res = (facts("Observation", "obs-1", value=Decimal("8.2"), unit="%"),)
    verdict = verify("A1c 8.2 [Observation/obs-1].", refs, resources=res)

    claim = only_claim(verdict)
    assert status_value(claim) == VERIFIED
    assert numeric_value(claim) == CHECKED


def test_probe_8_19_percent_vs_8_2_strips() -> None:
    refs = (ref("Observation", "obs-1"),)
    res = (facts("Observation", "obs-1", value=Decimal("8.2")),)  # bare value
    verdict = verify("A1c is 8.19% [Observation/obs-1].", refs, resources=res)

    assert status_value(only_claim(verdict)) == NUM_MISMATCH


def test_probe_8_20_percent_vs_8_2_passes() -> None:
    refs = (ref("Observation", "obs-1"),)
    res = (facts("Observation", "obs-1", value=Decimal("8.2")),)  # bare value
    verdict = verify("A1c is 8.20% [Observation/obs-1].", refs, resources=res)

    claim = only_claim(verdict)
    assert status_value(claim) == VERIFIED
    assert numeric_value(claim) == CHECKED


def test_probe_zero_date_claim_is_unchecked_no_exception() -> None:
    refs = (ref("Encounter", "enc-1"),)
    res = (facts("Encounter", "enc-1", date="0000-00-00"),)
    # Must not raise.
    verdict = verify(
        "The last visit was 2023-11-02 [Encounter/enc-1].", refs, resources=res
    )
    claim = only_claim(verdict)
    assert status_value(claim) == VERIFIED
    assert numeric_value(claim) == UNCHECKED
    assert claim.numeric.resource_date_unknown is True


def test_probe_two_citations_one_matches_one_contradicts_passes() -> None:
    # One quantity, two cited comparable resources; matches at least one → pass.
    refs = (ref("Observation", "obs-1"), ref("Observation", "obs-2"))
    res = (
        facts("Observation", "obs-1", value=Decimal("8.2"), unit="%"),
        facts("Observation", "obs-2", value=Decimal("7.1"), unit="%"),
    )
    verdict = verify(
        "A1c is 8.2% [Observation/obs-1] [Observation/obs-2].",
        refs,
        resources=res,
    )
    claim = only_claim(verdict)
    assert status_value(claim) == VERIFIED
    assert numeric_value(claim) == CHECKED


def test_probe_two_citations_neither_matches_both_comparable_strips() -> None:
    refs = (ref("Observation", "obs-1"), ref("Observation", "obs-2"))
    res = (
        facts("Observation", "obs-1", value=Decimal("8.2"), unit="%"),
        facts("Observation", "obs-2", value=Decimal("7.1"), unit="%"),
    )
    verdict = verify(
        "A1c is 6.0% [Observation/obs-1] [Observation/obs-2].",
        refs,
        resources=res,
    )
    assert status_value(only_claim(verdict)) == NUM_MISMATCH


def test_probe_no_numeric_survives_and_not_numerically_verified() -> None:
    refs = (ref("Condition", "cond-1"),)
    res = (facts("Condition", "cond-1"),)
    verdict = verify(
        "The patient has diabetes [Condition/cond-1].", refs, resources=res
    )
    claim = only_claim(verdict)
    assert status_value(claim) == VERIFIED
    assert numeric_value(claim) == UNCHECKED
    assert verdict.counts.numeric_checked == 0


# ==========================================================================
# Self-devised adversarial probes (beyond the mandatory list)
# ==========================================================================


def test_probe_mcg_mg_conversion_passes() -> None:
    # 500 mcg == 0.5 mg (the second table entry: mcg/mg).
    refs = (ref("Observation", "obs-1"),)
    res = (facts("Observation", "obs-1", value=Decimal("0.5"), unit="mg"),)
    verdict = verify("Dose is 500 mcg [Observation/obs-1].", refs, resources=res)

    claim = only_claim(verdict)
    assert status_value(claim) == VERIFIED
    assert numeric_value(claim) == CHECKED


def test_probe_mcg_mg_conversion_mismatch_strips() -> None:
    # 500 mcg == 0.5 mg, so 500 mcg vs 0.6 mg is a real contradiction.
    refs = (ref("Observation", "obs-1"),)
    res = (facts("Observation", "obs-1", value=Decimal("0.6"), unit="mg"),)
    verdict = verify("Dose is 500 mcg [Observation/obs-1].", refs, resources=res)

    assert status_value(only_claim(verdict)) == NUM_MISMATCH


def test_probe_percent_vs_mg_is_a_unit_dimension_mismatch_strips() -> None:
    # Same number, incompatible dimensions (% vs mass) → must NOT pass on value.
    refs = (ref("Observation", "obs-1"),)
    res = (facts("Observation", "obs-1", value=Decimal("8.2"), unit="mg"),)
    verdict = verify("A1c is 8.2% [Observation/obs-1].", refs, resources=res)

    assert status_value(only_claim(verdict)) == NUM_MISMATCH


def test_probe_mmol_l_matches_itself() -> None:
    refs = (ref("Observation", "obs-1"),)
    res = (facts("Observation", "obs-1", value=Decimal("5.8"), unit="mmol/L"),)
    verdict = verify(
        "Potassium is 5.8 mmol/L [Observation/obs-1].", refs, resources=res
    )
    claim = only_claim(verdict)
    assert status_value(claim) == VERIFIED
    assert numeric_value(claim) == CHECKED


def test_probe_citation_id_digits_are_not_treated_as_quantities() -> None:
    # "obs-1" contains a digit; it must not be compared as a claim quantity and
    # falsely mismatch the resource value.
    refs = (ref("Observation", "obs-1"),)
    res = (facts("Observation", "obs-1", value=Decimal("5")),)
    verdict = verify(
        "The reading is within range [Observation/obs-1].", refs, resources=res
    )
    claim = only_claim(verdict)
    assert status_value(claim) == VERIFIED
    assert numeric_value(claim) == UNCHECKED  # no real quantity in the prose


def test_probe_blood_pressure_ratio_does_not_falsely_strip() -> None:
    # "120/80" is a ratio, not two comparable scalars; it must not manufacture a
    # spurious mismatch against a cited resource.
    refs = (ref("Observation", "obs-1"),)
    res = (facts("Observation", "obs-1", value=Decimal("120"), unit="mmHg"),)
    verdict = verify(
        "Blood pressure is 120/80 mmHg [Observation/obs-1].", refs, resources=res
    )
    claim = only_claim(verdict)
    assert status_value(claim) != NUM_MISMATCH
    assert verdict.content_removed is False


def test_probe_multiple_quantities_one_contradiction_strips() -> None:
    # Two scalar quantities, one contradicts a comparable cited resource → strip.
    refs = (ref("Observation", "obs-1"),)
    res = (facts("Observation", "obs-1", value=Decimal("8.2"), unit="%"),)
    verdict = verify(
        "A1c is 8.2% but previously 7.1% [Observation/obs-1].",
        refs,
        resources=res,
    )
    assert status_value(only_claim(verdict)) == NUM_MISMATCH


def test_probe_iso_date_year_is_not_treated_as_a_numeric_quantity() -> None:
    # The "2024" in a date must be consumed by the date extractor, never left to
    # collide with a cited numeric resource value.
    refs = (ref("Encounter", "enc-1"),)
    res = (
        facts("Encounter", "enc-1", value=Decimal("3"), date="2024-03-15"),
    )
    verdict = verify(
        "The visit was on 2024-03-15 [Encounter/enc-1].", refs, resources=res
    )
    claim = only_claim(verdict)
    # Date matches; the year 2024 did not mismatch the value 3.
    assert status_value(claim) == VERIFIED
    assert numeric_value(claim) == CHECKED


def test_probe_uncited_resource_facts_are_never_compared() -> None:
    # A quantity is only checked against resources the sentence actually cites;
    # facts for an uncited resource must not create a mismatch.
    refs = (ref("Observation", "obs-1"), ref("Observation", "obs-2"))
    res = (
        facts("Observation", "obs-1", value=Decimal("8.2"), unit="%"),
        facts("Observation", "obs-2", value=Decimal("1.0"), unit="%"),
    )
    verdict = verify("A1c is 8.2% [Observation/obs-1].", refs, resources=res)

    claim = only_claim(verdict)
    assert status_value(claim) == VERIFIED
    assert numeric_value(claim) == CHECKED


def test_probe_all_claims_numeric_stripped_triggers_full_fallback() -> None:
    # Numeric mismatches are real strips: if every claim falls this way, the
    # fail-closed fallback still fires.
    refs = (ref("Observation", "obs-1"),)
    res = (facts("Observation", "obs-1", value=Decimal("8.2"), unit="%"),)
    verdict = verify("A1c is 7.1% [Observation/obs-1].", refs, resources=res)

    assert verdict.fallback_triggered is True
    assert verdict.output_text == verify_mod().FALLBACK_TEXT
    assert set(verdict.available_refs) == set(refs)
    assert verdict.counts.claims_stripped == 1
    assert verdict.counts.claims_passed == 0


def test_probe_grounding_failure_takes_precedence_over_numeric() -> None:
    # An unknown citation is stripped by grounding before any content check runs;
    # the status stays the grounding failure, not a numeric mismatch.
    refs = (ref("Observation", "obs-1"),)
    res = (facts("Observation", "obs-1", value=Decimal("8.2"), unit="%"),)
    verdict = verify("A1c is 7.1% [Observation/ghost-9].", refs, resources=res)

    claim = only_claim(verdict)
    assert status_value(claim) == "stripped_unknown_citation"


# ==========================================================================
# Rev 2 (orchestrator-mandated tightening) — mask non-measurement numbers
# ==========================================================================
#
# A bare integer in ordinary clinical prose (a diagnosis code, a disease
# classifier, a bare year) is not a measurement and must never be compared
# against a cited resource's valueQuantity. Deleting a true, correctly
# grounded claim this way is worse than a missed check: T008 falls back to the
# fallback text when no claim-bearing sentence survives, so a false strip on a
# one-claim summary silently degrades an entire correct answer to "couldn't
# verify."
#
# Every "survives" case below cites a resource that DOES carry a
# *contradicting* valueQuantity (8.2 %) — this proves the number in the claim
# text never reached the comparator at all, not merely that it reached the
# comparator and happened to pass.


def test_covid_19_hyphenated_compound_is_masked_and_survives() -> None:
    refs = (ref("Condition", "c1"),)
    res = (facts("Condition", "c1", value=Decimal("8.2"), unit="%"),)
    verdict = verify(
        "She was treated for COVID-19 [Condition/c1].", refs, resources=res
    )
    claim = only_claim(verdict)
    assert status_value(claim) == VERIFIED
    assert numeric_value(claim) == UNCHECKED


def test_type_2_diabetes_classifier_number_is_masked_and_survives() -> None:
    refs = (ref("Condition", "c1"),)
    res = (facts("Condition", "c1", value=Decimal("8.2"), unit="%"),)
    verdict = verify("She has Type 2 diabetes [Condition/c1].", refs, resources=res)
    claim = only_claim(verdict)
    assert status_value(claim) == VERIFIED
    assert numeric_value(claim) == UNCHECKED


def test_bare_diagnosis_year_is_masked_and_survives() -> None:
    refs = (ref("Condition", "c1"),)
    res = (facts("Condition", "c1", value=Decimal("8.2"), unit="%"),)
    verdict = verify(
        "She was diagnosed in 2019 [Condition/c1].", refs, resources=res
    )
    claim = only_claim(verdict)
    assert status_value(claim) == VERIFIED
    assert numeric_value(claim) == UNCHECKED


def test_stage_classifier_number_is_masked_and_survives() -> None:
    refs = (ref("Condition", "c1"),)
    res = (facts("Condition", "c1", value=Decimal("8.2"), unit="%"),)
    verdict = verify(
        "She has stage 3 chronic kidney disease [Condition/c1].",
        refs,
        resources=res,
    )
    claim = only_claim(verdict)
    assert status_value(claim) == VERIFIED
    assert numeric_value(claim) == UNCHECKED


def test_vitamin_b12_alphanumeric_token_survives() -> None:
    # No hyphen; the digit run is directly attached to a letter ("B12"), which
    # _QUANTITY_RE's own lookbehind already excludes from matching as a bare
    # quantity to begin with — this pins that it keeps working post-masking.
    refs = (ref("Condition", "c1"),)
    res = (facts("Condition", "c1", value=Decimal("8.2"), unit="%"),)
    verdict = verify(
        "She has vitamin B12 deficiency [Condition/c1].", refs, resources=res
    )
    claim = only_claim(verdict)
    assert status_value(claim) == VERIFIED
    assert numeric_value(claim) == UNCHECKED


# --- Self-devised additional masking cases (beyond the mandatory 5) ---


def test_sars_cov_2_hyphenated_compound_is_masked_and_survives() -> None:
    refs = (ref("Condition", "c1"),)
    res = (facts("Condition", "c1", value=Decimal("8.2"), unit="%"),)
    verdict = verify(
        "She tested positive for SARS-CoV-2 [Condition/c1].", refs, resources=res
    )
    claim = only_claim(verdict)
    assert status_value(claim) == VERIFIED
    assert numeric_value(claim) == UNCHECKED


def test_grade_classifier_number_is_masked_and_survives() -> None:
    refs = (ref("Condition", "c1"),)
    res = (facts("Condition", "c1", value=Decimal("8.2"), unit="%"),)
    verdict = verify(
        "The pathology showed grade 2 sarcoma [Condition/c1].",
        refs,
        resources=res,
    )
    claim = only_claim(verdict)
    assert status_value(claim) == VERIFIED
    assert numeric_value(claim) == UNCHECKED


def test_phase_classifier_number_is_masked_and_survives() -> None:
    refs = (ref("Condition", "c1"),)
    res = (facts("Condition", "c1", value=Decimal("8.2"), unit="%"),)
    verdict = verify(
        "She is enrolled in a phase 2 trial [Condition/c1].", refs, resources=res
    )
    claim = only_claim(verdict)
    assert status_value(claim) == VERIFIED
    assert numeric_value(claim) == UNCHECKED


def test_bare_year_with_adjacent_unit_still_compares_as_a_real_quantity() -> None:
    # The "no adjacent unit" carve-out on the bare-year mask: a 4-digit number
    # directly followed by a real unit is still a measurement candidate and is
    # NOT masked — it is compared normally.
    refs = (ref("Observation", "obs-1"),)
    res = (facts("Observation", "obs-1", value=Decimal("2019"), unit="mg"),)
    verdict = verify("Dose is 2019 mg [Observation/obs-1].", refs, resources=res)
    claim = only_claim(verdict)
    assert status_value(claim) == VERIFIED
    assert numeric_value(claim) == CHECKED


# --- Regression guards: masking must never launder a real contradiction ---
# The bias is explicit: a false survive (a fabricated value passing) is more
# dangerous than a false strip. Masking must never turn a genuine value
# mismatch into `unchecked` — that would be a worse defect than the one it
# fixes.


def test_regression_percent_value_mismatch_still_strips() -> None:
    refs = (ref("Observation", "obs-1"),)
    res = (facts("Observation", "obs-1", value=Decimal("8.2"), unit="%"),)
    verdict = verify("A1c is 7.1% [Observation/obs-1].", refs, resources=res)
    assert status_value(only_claim(verdict)) == NUM_MISMATCH


def test_regression_bare_value_mismatch_still_strips() -> None:
    refs = (ref("Observation", "obs-1"),)
    res = (facts("Observation", "obs-1", value=Decimal("8.2")),)
    verdict = verify("A1c is 7.1 [Observation/obs-1].", refs, resources=res)
    assert status_value(only_claim(verdict)) == NUM_MISMATCH


def test_regression_nonconvertible_unit_mismatch_still_strips() -> None:
    refs = (ref("Observation", "obs-1"),)
    res = (facts("Observation", "obs-1", value=Decimal("150"), unit="mL"),)
    verdict = verify(
        "The dose is 150 mg [Observation/obs-1].", refs, resources=res
    )
    assert status_value(only_claim(verdict)) == NUM_MISMATCH
