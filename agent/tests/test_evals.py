"""Tests for the eval-case fixture schema, loader, and runner (T015).

ARCHITECTURE.md section 8: eval cases live as versioned fixtures in the repo
(the platform is only a runner/viewer); every case exercises a boundary, an
invariant, or a regression risk. This ticket builds the deterministic
harness — the schema, the loader, the check registry, and the runner that
drives a case end-to-end against the in-process app with a scripted LLM and
a mock FHIR transport. No network, ever.

Criteria map (see .tdd-swarm/tickets/T015-eval-fixtures.md):
  1. A Pydantic eval-case schema (id, guards_against, scenario, named checks);
     a loader validates every file in a directory (malformed case -> named
     load error, asserted).
  2. A runner executes a case end-to-end (mock FHIR transport from fixture
     data; scripted LLM behind T010's port) and evaluates the named checks,
     producing a machine-readable per-case pass/fail report with reasons.
  3. The schema requires a non-empty ``failure_mode`` per case; the loader
     rejects cases without it.
  4. Seed cases shipped and passing, covering: empty patient record
     (boundary); no labs (boundary); no-allergy-row absence scoping
     (data-quality boundary); med-status conflict (regression pin, T007
     Lisinopril case); uncited-claim injection (invariant); other-patient
     query refused (invariant); tool failure discloses in coverage
     (invariant).
  5. Invocable as a CLI (``python -m copilot.evals``) exiting non-zero on any
     failing case.

The governing design decision (orchestrator-mandated, non-negotiable): every
mechanism here must be *observed to fail* on a known-bad input before it is
trusted on a good one. A suite that cannot be shown to fail proves nothing.

Production code is imported lazily inside test bodies (via the ``*_mod()``
accessors below) so collection succeeds before the implementation exists —
RED is the missing feature, never an import error at collection time.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Lazy module accessors for the genuinely-new eval package
# ---------------------------------------------------------------------------


def schema_mod() -> Any:
    from copilot.evals import schema

    return schema


def loader_mod() -> Any:
    from copilot.evals import loader

    return loader


def checks_mod() -> Any:
    from copilot.evals import checks

    return checks


def runner_mod() -> Any:
    from copilot.evals import runner

    return runner


def results_mod() -> Any:
    from copilot.evals import results

    return results


def fhir_mock_mod() -> Any:
    from copilot.evals import fhir_mock

    return fhir_mock


# ---------------------------------------------------------------------------
# Case-dict builders (raw, JSON-serializable — no production import needed to
# construct these; only to load/run them)
# ---------------------------------------------------------------------------


def minimal_case_dict(
    *,
    case_id: str = "case-1",
    guards_against: str = "invariant",
    failure_mode: str | None = "a genuine failure mode this case guards against",
    patient_id: str = "pat-1",
    fhir_fixture: dict[str, Any] | None = None,
    fhir_failures: list[str] | None = None,
    messages: list[str] | None = None,
    llm_script: list[dict[str, Any]] | None = None,
    checks: list[dict[str, Any]] | None = None,
    omit_failure_mode: bool = False,
) -> dict[str, Any]:
    """A well-formed case dict whose defaults produce a trivially-passing run:
    a single scaffolding reply (a greeting — no citation needed) checked by
    ``absence_answer_scoped``'s ``contains`` param against that same text."""
    case: dict[str, Any] = {
        "id": case_id,
        "guards_against": guards_against,
        "scenario": {
            "patient_id": patient_id,
            "fhir_fixture": fhir_fixture or {},
            "fhir_failures": fhir_failures or [],
            "messages": messages or ["Catch me up."],
            "llm_script": llm_script
            or [{"stop_reason": "end_turn", "text": "Hello, I reviewed the chart."}],
        },
        "checks": checks
        or [
            {
                "name": "absence_answer_scoped",
                "params": {"contains": "Hello, I reviewed the chart."},
            }
        ],
    }
    if not omit_failure_mode:
        case["failure_mode"] = failure_mode
    return case


def write_json_case(directory: Path, filename: str, case: dict[str, Any]) -> Path:
    path = directory / filename
    path.write_text(json.dumps(case))
    return path


def tool_use(call_id: str, name: str, **arguments: Any) -> dict[str, Any]:
    return {
        "stop_reason": "tool_use",
        "tool_calls": [{"id": call_id, "name": name, "arguments": arguments}],
    }


def end_turn(text: str) -> dict[str, Any]:
    return {"stop_reason": "end_turn", "text": text}


def refusal() -> dict[str, Any]:
    return {"stop_reason": "refusal"}


def build_case(case_dict: dict[str, Any]) -> Any:
    """Construct an ``EvalCase`` directly from a dict via the real schema,
    bypassing the filesystem loader — used for in-memory runner/check tests
    that don't need to exercise file-loading mechanics."""
    return schema_mod().EvalCase.model_validate(case_dict)


# ===========================================================================
# Criterion 1 + criterion 3 + mandatory — schema/loader, five named errors
# ===========================================================================


def test_well_formed_case_loads_with_expected_shape(tmp_path: Path) -> None:
    write_json_case(tmp_path, "case.json", minimal_case_dict())
    cases = loader_mod().load_cases(tmp_path)

    assert len(cases) == 1
    case = cases[0]
    assert case.id == "case-1"
    assert case.guards_against == schema_mod().GuardsAgainst.INVARIANT
    assert case.failure_mode == "a genuine failure mode this case guards against"
    assert case.scenario.patient_id == "pat-1"
    assert case.scenario.messages == ("Catch me up.",)
    assert len(case.scenario.llm_script) == 1
    assert case.checks[0].name == "absence_answer_scoped"


def test_valid_minimal_yaml_case_parses_via_real_yaml(tmp_path: Path) -> None:
    """Proves the loader really parses YAML (not merely JSON) end to end."""
    yaml_text = (
        "id: yaml-case-1\n"
        "guards_against: invariant\n"
        'failure_mode: "a genuine failure mode description"\n'
        "scenario:\n"
        "  patient_id: pat-1\n"
        "  messages:\n"
        '    - "Catch me up."\n'
        "  llm_script:\n"
        "    - stop_reason: end_turn\n"
        '      text: "Hello, I reviewed the chart."\n'
        "checks:\n"
        "  - name: absence_answer_scoped\n"
        "    params:\n"
        '      contains: "Hello, I reviewed the chart."\n'
    )
    (tmp_path / "yaml_case.yaml").write_text(yaml_text)

    cases = loader_mod().load_cases(tmp_path)

    assert len(cases) == 1
    assert cases[0].id == "yaml-case-1"
    assert cases[0].scenario.messages == ("Catch me up.",)


def test_malformed_yaml_raises_named_load_error_naming_file(tmp_path: Path) -> None:
    """Loader error 1/5: malformed YAML (a literal tab cannot start a YAML
    token) fails loudly, naming the offending file."""
    bad_file = tmp_path / "broken.yaml"
    bad_file.write_text("id: bad-case\nscenario:\n\tpatient_id: pat-1\n")

    lmod = loader_mod()
    try:
        lmod.load_cases(tmp_path)
        raise AssertionError("expected an EvalLoadError for malformed YAML")
    except lmod.EvalLoadError as exc:
        assert exc.kind == lmod.LoadErrorKind.MALFORMED_YAML
        assert exc.file == bad_file
        assert str(bad_file) in str(exc)


def test_missing_failure_mode_raises_named_load_error_naming_file(
    tmp_path: Path,
) -> None:
    """Loader error 2/5: a case with no ``failure_mode`` key at all."""
    case = minimal_case_dict(omit_failure_mode=True)
    path = write_json_case(tmp_path, "missing_fm.json", case)

    lmod = loader_mod()
    try:
        lmod.load_cases(tmp_path)
        raise AssertionError("expected an EvalLoadError for missing failure_mode")
    except lmod.EvalLoadError as exc:
        assert exc.kind == lmod.LoadErrorKind.MISSING_FAILURE_MODE
        assert exc.file == path
        assert str(path) in str(exc)


def test_empty_failure_mode_raises_named_load_error_naming_file(
    tmp_path: Path,
) -> None:
    """Loader error 3/5: a case whose ``failure_mode`` is an empty string."""
    case = minimal_case_dict(failure_mode="")
    path = write_json_case(tmp_path, "empty_fm.json", case)

    lmod = loader_mod()
    try:
        lmod.load_cases(tmp_path)
        raise AssertionError("expected an EvalLoadError for empty failure_mode")
    except lmod.EvalLoadError as exc:
        assert exc.kind == lmod.LoadErrorKind.EMPTY_FAILURE_MODE
        assert exc.file == path
        assert str(path) in str(exc)


def test_unknown_check_name_raises_named_load_error_naming_file_and_check(
    tmp_path: Path,
) -> None:
    """Loader error 4/5 + mandatory adversarial: an unregistered check name
    fails loudly, naming both the file and the offending check name."""
    case = minimal_case_dict(
        checks=[{"name": "totally_bogus_check_xyz", "params": {}}]
    )
    path = write_json_case(tmp_path, "unknown_check.json", case)

    lmod = loader_mod()
    try:
        lmod.load_cases(tmp_path)
        raise AssertionError("expected an EvalLoadError for an unknown check")
    except lmod.EvalLoadError as exc:
        assert exc.kind == lmod.LoadErrorKind.UNKNOWN_CHECK
        assert exc.file == path
        assert exc.check_name == "totally_bogus_check_xyz"
        assert str(path) in str(exc)
        assert "totally_bogus_check_xyz" in str(exc)


def test_duplicate_case_id_raises_named_load_error_naming_second_file(
    tmp_path: Path,
) -> None:
    """Loader error 5/5: two files declaring the same case id."""
    write_json_case(tmp_path, "a_first.json", minimal_case_dict(case_id="dup-id"))
    second_path = write_json_case(
        tmp_path, "b_second.json", minimal_case_dict(case_id="dup-id")
    )

    lmod = loader_mod()
    try:
        lmod.load_cases(tmp_path)
        raise AssertionError("expected an EvalLoadError for a duplicate case id")
    except lmod.EvalLoadError as exc:
        assert exc.kind == lmod.LoadErrorKind.DUPLICATE_ID
        assert exc.file == second_path
        assert "dup-id" in str(exc)


def test_reserved_entailment_check_loads_without_a_load_error(
    tmp_path: Path,
) -> None:
    """The reserved, unimplemented ``entailment_judge`` check is *registered*
    — referencing it must never raise a load error (it fails at run time
    instead, per the design decision — see the runner test below)."""
    case = minimal_case_dict(checks=[{"name": "entailment_judge", "params": {}}])
    write_json_case(tmp_path, "entailment.json", case)

    cases = loader_mod().load_cases(tmp_path)

    assert len(cases) == 1
    assert cases[0].checks[0].name == "entailment_judge"


def test_nonexistent_directory_yields_zero_cases_not_an_error(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    cases = loader_mod().load_cases(missing)
    assert cases == ()


def test_failure_mode_is_required_directly_on_the_pydantic_schema() -> None:
    """Criterion 3, at the schema layer (not just the loader): constructing
    an ``EvalCase`` without ``failure_mode`` raises a Pydantic error."""
    import pydantic

    smod = schema_mod()
    case_dict = minimal_case_dict(omit_failure_mode=True)
    try:
        smod.EvalCase.model_validate(case_dict)
        raise AssertionError("expected a pydantic ValidationError")
    except pydantic.ValidationError as exc:
        assert any(err["loc"] == ("failure_mode",) for err in exc.errors())


# ===========================================================================
# Criterion 2 — runner executes end-to-end; machine-readable report
# ===========================================================================


async def test_runner_executes_a_case_end_to_end_and_produces_a_pass_report() -> None:
    case = build_case(minimal_case_dict())
    report = await runner_mod().run_case(case)

    assert report.passed is True
    assert report.reasons == ()
    assert report.id == "case-1"
    assert report.guards_against == schema_mod().GuardsAgainst.INVARIANT


async def test_case_report_is_machine_readable_with_required_fields() -> None:
    case = build_case(minimal_case_dict())
    report = await runner_mod().run_case(case)
    dumped = report.model_dump(mode="json")

    assert set(dumped.keys()) >= {"id", "guards_against", "passed", "reasons"}
    assert dumped["passed"] is True
    assert dumped["reasons"] == []
    json.dumps(dumped)  # must be JSON-serializable as-is


async def test_runner_drives_the_scripted_llm_through_the_real_agent_loop() -> None:
    """The run really went through T010's ``AgentLoop`` (not a stub): the
    observed output text is exactly what the scripted LLM produced, and the
    turn is a genuine (non-fallback, non-degraded) answer."""
    case = build_case(
        minimal_case_dict(
            llm_script=[end_turn("Hello, distinctive marker 12345.")],
            checks=[
                {
                    "name": "absence_answer_scoped",
                    "params": {"contains": "Hello, distinctive marker 12345."},
                }
            ],
        )
    )
    response = await runner_mod().execute_case(case)

    assert response.last.is_fallback is False
    assert response.last.is_degraded is False
    assert "distinctive marker 12345" in response.last.output_text


# ===========================================================================
# Criterion 4 — seed cases shipped and passing
# ===========================================================================


def _seed_cases_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "evals" / "cases"


async def test_all_seed_cases_load_and_pass() -> None:
    lmod = loader_mod()
    rmod = runner_mod()
    cases = lmod.load_cases(_seed_cases_dir())

    assert len(cases) >= 7, "expected at least the 7 mandated seed cases"

    reports = await rmod.run_cases(cases)
    failures = [(r.id, r.reasons) for r in reports if not r.passed]
    assert failures == [], f"seed cases must all pass: {failures}"


async def test_seed_cases_cover_boundary_invariant_and_regression() -> None:
    smod = schema_mod()
    cases = loader_mod().load_cases(_seed_cases_dir())
    seen = {case.guards_against for case in cases}
    assert seen == {
        smod.GuardsAgainst.BOUNDARY,
        smod.GuardsAgainst.INVARIANT,
        smod.GuardsAgainst.REGRESSION,
    }


async def test_seed_cases_each_have_a_nonempty_failure_mode() -> None:
    cases = loader_mod().load_cases(_seed_cases_dir())
    for case in cases:
        assert case.failure_mode.strip(), f"{case.id} has no failure_mode"


# ===========================================================================
# Criterion 5 + mandatory — CLI subprocess, real exit codes
# ===========================================================================


def run_cli(cases_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "copilot.evals", str(cases_dir)],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_cli_exits_zero_when_all_cases_pass(tmp_path: Path) -> None:
    write_json_case(tmp_path, "pass.json", minimal_case_dict(case_id="pass-case"))
    result = run_cli(tmp_path)

    assert result.returncode == 0, (result.stdout, result.stderr)
    report = json.loads(result.stdout)
    assert report["total"] == 1
    assert report["passed"] == 1
    assert report["failed"] == 0


def test_cli_exits_nonzero_for_one_failing_case(tmp_path: Path) -> None:
    # A model that immediately refuses, but the case (wrongly) expects an
    # ordinary absence-scoped answer — a genuine, real failure.
    failing = minimal_case_dict(
        case_id="fail-case",
        llm_script=[refusal()],
        checks=[{"name": "absence_answer_scoped", "params": {"contains": "no"}}],
    )
    write_json_case(tmp_path, "fail.json", failing)
    result = run_cli(tmp_path)

    assert result.returncode != 0, (result.stdout, result.stderr)
    report = json.loads(result.stdout)
    assert report["total"] == 1
    assert report["failed"] == 1
    assert report["cases"][0]["passed"] is False
    assert report["cases"][0]["reasons"]


def test_cli_exits_nonzero_for_a_load_error(tmp_path: Path) -> None:
    (tmp_path / "broken.yaml").write_text("id: bad\nscenario:\n\tpatient_id: pat-1\n")
    result = run_cli(tmp_path)

    assert result.returncode != 0, (result.stdout, result.stderr)
    report = json.loads(result.stdout)
    assert "error" in report
    assert "broken.yaml" in report["file"]


def test_cli_exits_nonzero_for_an_empty_cases_directory(tmp_path: Path) -> None:
    result = run_cli(tmp_path)  # tmp_path exists but has zero fixture files

    assert result.returncode != 0, (result.stdout, result.stderr)
    report = json.loads(result.stdout)
    assert report.get("total", 0) == 0


def test_cli_json_report_on_stdout_and_human_summary_on_stderr(
    tmp_path: Path,
) -> None:
    write_json_case(tmp_path, "pass.json", minimal_case_dict(case_id="stdout-case"))
    result = run_cli(tmp_path)

    # stdout is pure JSON — parses as exactly one object, nothing else mixed in.
    report = json.loads(result.stdout)
    assert report["cases"][0]["id"] == "stdout-case"
    # stderr carries a human-readable summary, never raw JSON as its only content.
    assert "stdout-case" in result.stderr
    assert "cases passed" in result.stderr


def test_cli_import_pulls_no_anthropic_in_a_clean_subprocess() -> None:
    """No network, ever: importing the eval runner must not pull the
    Anthropic SDK (mirrors T010/T014's import-purity idiom)."""
    code = (
        "import sys\n"
        "import copilot.evals.runner\n"
        "assert 'anthropic' not in sys.modules, sys.modules.keys()\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "OK" in result.stdout


# ===========================================================================
# Mandatory adversarial — the runner fails a case it should fail
# ===========================================================================


async def test_runner_fails_a_mutated_seed_case_naming_the_check() -> None:
    """Take a case that genuinely passes (the model answers normally), mutate
    its expectation to something the system does not do (expects a refusal),
    and assert the runner reports fail with a reason naming the check."""
    passing = minimal_case_dict(
        case_id="mutation-target",
        llm_script=[end_turn("Hello, I reviewed the chart.")],
        checks=[
            {
                "name": "absence_answer_scoped",
                "params": {"contains": "Hello, I reviewed the chart."},
            }
        ],
    )
    baseline_report = await runner_mod().run_case(build_case(passing))
    assert baseline_report.passed is True, "the baseline case must genuinely pass first"

    mutated = dict(passing)
    mutated["checks"] = [{"name": "refusal_expected", "params": {}}]
    mutated_report = await runner_mod().run_case(build_case(mutated))

    assert mutated_report.passed is False
    assert len(mutated_report.reasons) == 1
    assert mutated_report.reasons[0].startswith("refusal_expected:")


async def test_reserved_entailment_check_case_fails_never_passes_never_skipped() -> (
    None
):
    """Mandatory: referencing the reserved, unimplemented entailment check
    must fail the case — never pass, never a vacuous zero-checks skip."""
    case = build_case(
        minimal_case_dict(
            case_id="entailment-case",
            checks=[{"name": "entailment_judge", "params": {}}],
        )
    )
    report = await runner_mod().run_case(case)

    assert report.passed is False
    assert len(report.reasons) == 1
    assert report.reasons[0].startswith("entailment_judge:")
    assert "not implemented" in report.reasons[0] or "reserved" in report.reasons[0]


# ===========================================================================
# Mandatory adversarial — Lisinopril regression pin (AUDIT.md D3 / T007)
# ===========================================================================

_LISINOPRIL_CONFLICT_FIXTURE = {
    "Patient": [
        {"resourceType": "Patient", "id": "pat-2", "name": [{"text": "Jane Roe"}]}
    ],
    "MedicationRequest": [
        {
            "resourceType": "MedicationRequest",
            "id": "rx-lisinopril-active",
            "status": "active",
            "medicationCodeableConcept": {"text": "Lisinopril 10mg"},
        },
        {
            "resourceType": "MedicationRequest",
            "id": "rx-lisinopril-inactive",
            "status": "completed",
            "medicationCodeableConcept": {"text": "Lisinopril 10mg"},
        },
    ],
    "Condition": [],
    "AllergyIntolerance": [],
    "Observation": [],
    "Encounter": [],
}


async def test_lisinopril_conflict_is_flagged_regression_pin() -> None:
    """AUDIT.md D3 / T007: a same-drug active/inactive disagreement across
    records is flagged, never silently resolved to one status. If this ever
    passes while the flag is absent, the check itself is wrong (see the
    adversarial companion test below, which proves the check can fail)."""
    case = build_case(
        {
            "id": "lisinopril-regression",
            "guards_against": "regression",
            "failure_mode": (
                "medication status conflict across records silently resolved "
                "to one status instead of being flagged (T007/AUDIT.md D3)"
            ),
            "scenario": {
                "patient_id": "pat-2",
                "fhir_fixture": _LISINOPRIL_CONFLICT_FIXTURE,
                "messages": ["What medications is she on?"],
                "llm_script": [
                    tool_use("tc-1", "get_patient_snapshot"),
                    end_turn(
                        "Lisinopril's status disagrees across records: active "
                        "in one and inactive in another "
                        "[MedicationRequest/rx-lisinopril-active]"
                        "[MedicationRequest/rx-lisinopril-inactive]."
                    ),
                ],
            },
            "checks": [
                {
                    "name": "conflict_flagged",
                    "params": {"medication": "Lisinopril 10mg"},
                }
            ],
        }
    )
    report = await runner_mod().run_case(case)

    assert report.passed is True, report.reasons


async def test_conflict_flagged_check_fails_when_no_real_conflict_exists() -> None:
    """Adversarial companion (my own + reinforcing the mandate): the SAME
    check, run against a fixture with only a single, uncontested active
    Lisinopril record, must fail — proving the check can actually detect
    absence of a conflict rather than trivially always passing."""
    no_conflict_fixture = {
        "Patient": _LISINOPRIL_CONFLICT_FIXTURE["Patient"],
        "MedicationRequest": [_LISINOPRIL_CONFLICT_FIXTURE["MedicationRequest"][0]],
        "Condition": [],
        "AllergyIntolerance": [],
        "Observation": [],
        "Encounter": [],
    }
    case = build_case(
        {
            "id": "lisinopril-no-conflict",
            "guards_against": "regression",
            "failure_mode": "sanity check that conflict_flagged can actually fail",
            "scenario": {
                "patient_id": "pat-2",
                "fhir_fixture": no_conflict_fixture,
                "messages": ["What medications is she on?"],
                "llm_script": [
                    tool_use("tc-1", "get_patient_snapshot"),
                    end_turn(
                        "She is on Lisinopril [MedicationRequest/rx-lisinopril-active]."
                    ),
                ],
            },
            "checks": [
                {
                    "name": "conflict_flagged",
                    "params": {"medication": "Lisinopril 10mg"},
                }
            ],
        }
    )
    report = await runner_mod().run_case(case)

    assert report.passed is False
    assert report.reasons[0].startswith("conflict_flagged:")


# ===========================================================================
# Mandatory adversarial — T009 non-measurement mask / fabricated citation
# ===========================================================================


async def test_type2_diabetes_claim_survives_ghost_citation_is_stripped() -> None:
    """A claim containing "Type 2 diabetes" (properly cited) survives; a
    claim citing a fabricated ``[Observation/ghost]`` resource is stripped."""
    fixture = {
        "Patient": [
            {"resourceType": "Patient", "id": "pat-1", "name": [{"text": "Jane Doe"}]}
        ],
        "Condition": [
            {
                "resourceType": "Condition",
                "id": "cond-1",
                "code": {"text": "Type 2 diabetes mellitus"},
            }
        ],
        "MedicationRequest": [],
        "AllergyIntolerance": [],
        "Observation": [],
        "Encounter": [],
    }
    case = build_case(
        {
            "id": "type2-diabetes-ghost-citation",
            "guards_against": "invariant",
            "failure_mode": (
                "a fabricated citation survives verification, or a validly "
                "cited non-measurement claim is wrongly stripped"
            ),
            "scenario": {
                "patient_id": "pat-1",
                "fhir_fixture": fixture,
                "messages": ["Catch me up."],
                "llm_script": [
                    tool_use("tc-1", "get_patient_snapshot"),
                    end_turn(
                        "She has Type 2 diabetes [Condition/cond-1]. She has a "
                        "rare condition noted elsewhere [Observation/ghost]."
                    ),
                ],
            },
            "checks": [
                {
                    "name": "uncited_claim_stripped",
                    "params": {"not_contains": "rare condition noted elsewhere"},
                }
            ],
        }
    )
    response = await runner_mod().execute_case(case)
    reply = response.last.output_text

    assert "Type 2 diabetes" in reply
    assert "[Observation/ghost]" not in reply
    assert "rare condition noted elsewhere" not in reply

    report = await runner_mod().run_case(case)
    assert report.passed is True, report.reasons


# ===========================================================================
# My own adversarial probes (beyond the mandated list)
# ===========================================================================


async def test_coverage_discloses_failure_check_fails_on_status_mismatch() -> None:
    """My own: the check must be able to fail, not just pass — a category
    that actually succeeded (status "ok") must fail an expectation of
    "unavailable"."""
    fixture = {
        "Patient": [
            {"resourceType": "Patient", "id": "pat-1", "name": [{"text": "Jane Doe"}]}
        ],
        "AllergyIntolerance": [
            {
                "resourceType": "AllergyIntolerance",
                "id": "allergy-1",
                "code": {"text": "Penicillin"},
            }
        ],
        "MedicationRequest": [],
        "Condition": [],
        "Observation": [],
        "Encounter": [],
    }
    case = build_case(
        {
            "id": "coverage-mismatch",
            "guards_against": "boundary",
            "failure_mode": "sanity check that coverage_discloses_failure can fail",
            "scenario": {
                "patient_id": "pat-1",
                "fhir_fixture": fixture,
                "messages": ["Any allergies?"],
                "llm_script": [
                    tool_use("tc-1", "get_patient_snapshot"),
                    end_turn("She has an allergy [AllergyIntolerance/allergy-1]."),
                ],
            },
            "checks": [
                {
                    "name": "coverage_discloses_failure",
                    # allergies actually succeeded (status "ok"), so expecting
                    # "unavailable" here must genuinely fail.
                    "params": {"category": "allergies", "expected_status": "unavailable"},
                }
            ],
        }
    )
    report = await runner_mod().run_case(case)

    assert report.passed is False
    assert "coverage_discloses_failure:" in report.reasons[0]
    assert "allergies" in report.reasons[0]


async def test_multiturn_conversation_carries_history_across_turns() -> None:
    """My own: the runner supports more than one scripted message per case,
    replaying prior turns into the next turn's LLM call exactly like a real
    multi-turn conversation (T011/T012's history-replay rule: only a real,
    non-fallback/non-degraded turn's user+assistant text is carried
    forward)."""
    case = build_case(
        minimal_case_dict(
            case_id="multi-turn",
            messages=["Catch me up.", "What about her allergies?"],
            llm_script=[
                end_turn("Hello, I reviewed the chart."),
                end_turn("Hello again, allergies reviewed too."),
            ],
            checks=[
                {
                    "name": "absence_answer_scoped",
                    "params": {"contains": "Hello again, allergies reviewed too."},
                }
            ],
        )
    )
    response = await runner_mod().execute_case(case)

    assert len(response.turns) == 2
    assert response.turns[0].output_text == "Hello, I reviewed the chart."
    assert response.turns[1].output_text == "Hello again, allergies reviewed too."

    # The FIRST LLM call carries no prior history (nothing preceded it).
    assert len(response.llm_calls) == 2
    first_call_contents = [m.content for m in response.llm_calls[0]]
    assert not any("Hello, I reviewed" in c for c in first_call_contents)

    # The SECOND LLM call replays turn 1's user question and its answer.
    second_call_contents = [m.content for m in response.llm_calls[1]]
    assert any("Catch me up." in c for c in second_call_contents), second_call_contents
    assert any(
        "Hello, I reviewed the chart." in c for c in second_call_contents
    ), second_call_contents


async def test_case_with_multiple_checks_fails_overall_naming_only_the_failing_one() -> (
    None
):
    """My own: a case with several checks aggregates correctly — one failing
    check fails the whole case, and the report names only the failing check,
    not the passing ones."""
    case = build_case(
        minimal_case_dict(
            case_id="multi-check",
            llm_script=[end_turn("Hello, I reviewed the chart.")],
            checks=[
                {
                    "name": "absence_answer_scoped",
                    "params": {"contains": "Hello, I reviewed the chart."},
                },
                {
                    "name": "absence_answer_scoped",
                    "params": {"contains": "this text was never produced"},
                },
            ],
        )
    )
    report = await runner_mod().run_case(case)

    assert report.passed is False
    assert len(report.reasons) == 1
    assert "this text was never produced" in report.reasons[0]


async def test_tool_call_failure_surfaces_in_coverage_never_silently_dropped() -> None:
    """My own: a tool category that genuinely fails (mock 500) must disclose
    'unavailable' in the actual recorded snapshot output, proving
    ``fhir_failures`` really provokes a typed failure through the real FHIR
    client rather than merely an empty result."""
    fixture = {
        "Patient": [
            {"resourceType": "Patient", "id": "pat-1", "name": [{"text": "Jane Doe"}]}
        ],
        "MedicationRequest": [],
        "Condition": [],
        "AllergyIntolerance": [],
        "Observation": [],
        "Encounter": [],
    }
    case = build_case(
        minimal_case_dict(
            case_id="tool-failure",
            fhir_fixture=fixture,
            fhir_failures=["Observation"],
            llm_script=[
                tool_use("tc-1", "get_patient_snapshot"),
                end_turn("Here is what I found."),
            ],
            checks=[
                {
                    "name": "coverage_discloses_failure",
                    "params": {"category": "labs", "expected_status": "unavailable"},
                }
            ],
        )
    )
    report = await runner_mod().run_case(case)

    assert report.passed is True, report.reasons

    response = await runner_mod().execute_case(case)
    snapshots = response.tool_results("get_patient_snapshot")
    assert snapshots, "get_patient_snapshot was never actually called"
    labs_entry = next(c for c in snapshots[-1].coverage if c.category == "labs")
    assert labs_entry.status == "unavailable"
    assert "FhirUpstreamError" in labs_entry.reason or "500" in labs_entry.reason
