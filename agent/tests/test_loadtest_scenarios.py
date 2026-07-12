"""Tests for the load-test scenario definitions (T030 criterion 5).

Criterion 5: "Scenarios are realistic, not synthetic hot-loops. At least
one snapshot (UC-1) and one follow-up (UC-2) path; documented think-time."
"""

from __future__ import annotations

from copilot.loadtest.scenarios import SCENARIOS, UC1_SNAPSHOT, UC2_FOLLOWUP, Scenario


def test_uc1_is_a_single_snapshot_turn_with_no_followup() -> None:
    assert UC1_SNAPSHOT.name == "UC-1-snapshot"
    assert len(UC1_SNAPSHOT.steps) == 1
    assert UC1_SNAPSHOT.steps[0].is_followup is False


def test_uc2_is_a_snapshot_then_a_followup_in_the_same_conversation() -> None:
    assert UC2_FOLLOWUP.name == "UC-2-followup"
    assert len(UC2_FOLLOWUP.steps) == 2
    assert UC2_FOLLOWUP.steps[0].is_followup is False
    assert UC2_FOLLOWUP.steps[1].is_followup is True


def test_both_use_case_scenarios_are_registered() -> None:
    names = {s.name for s in SCENARIOS}
    assert "UC-1-snapshot" in names
    assert "UC-2-followup" in names


def test_default_think_time_is_within_the_documented_clinician_cadence() -> None:
    # Design decision: 20-30s think-time between turns.
    for scenario in SCENARIOS:
        assert 20.0 <= scenario.think_time_s <= 30.0


def test_with_think_time_returns_a_new_scenario_without_mutating_the_original() -> None:
    fast = UC2_FOLLOWUP.with_think_time(0.0)

    assert fast.think_time_s == 0.0
    assert fast is not UC2_FOLLOWUP
    assert UC2_FOLLOWUP.think_time_s != 0.0  # original untouched (immutability)
    assert fast.steps == UC2_FOLLOWUP.steps


def test_scenario_is_frozen() -> None:
    import pytest

    with pytest.raises(Exception):
        UC1_SNAPSHOT.think_time_s = 1.0  # type: ignore[misc]


def test_scenario_steps_carry_nonempty_messages() -> None:
    for scenario in SCENARIOS:
        for step in scenario.steps:
            assert step.message.strip()
            assert step.label.strip()


def test_custom_scenario_construction() -> None:
    from copilot.loadtest.scenarios import ScenarioStep

    s = Scenario(
        name="custom",
        steps=(ScenarioStep(label="a", message="hi"),),
        think_time_s=5.0,
    )
    assert s.name == "custom"
    assert s.steps[0].message == "hi"
