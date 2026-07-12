"""Realistic `/chat` load-test scenarios (T030 criterion 5).

Not synthetic hot-loops: at least one snapshot path (UC-1, a first turn with
no prior conversation) and one follow-up path (UC-2, a second turn that
reuses the conversation and — critically, per T040 — the same bearer
token). Think-time between a virtual user's turns models real clinician
cadence (documented default: 25s, within the ticket's 20-30s guidance) —
see ``copilot.loadtest.runner`` for where it is applied and
``docs/perf/README.md`` for the value actually used in recorded runs.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

#: Design-decision default: 20-30s clinician cadence between turns
#: (T030 ticket, "realistic think-time"). Smoke runs override this via
#: ``Scenario.with_think_time`` to keep local iteration fast.
DEFAULT_THINK_TIME_S = 25.0


@dataclass(frozen=True)
class ScenarioStep:
    """One turn a virtual user sends.

    ``is_followup`` marks a turn that must reuse the conversation started
    by an earlier step in the same scenario — the runner threads the
    ``conversation_id`` (and, structurally, the same bearer token) through
    for exactly these steps.
    """

    label: str
    message: str
    is_followup: bool = False


@dataclass(frozen=True)
class Scenario:
    name: str
    steps: tuple[ScenarioStep, ...]
    think_time_s: float = DEFAULT_THINK_TIME_S

    def with_think_time(self, think_time_s: float) -> "Scenario":
        """Return a copy with a different think-time (e.g. for a fast smoke run)."""
        return replace(self, think_time_s=think_time_s)


#: UC-1 — a single snapshot turn, no prior conversation.
UC1_SNAPSHOT = Scenario(
    name="UC-1-snapshot",
    steps=(
        ScenarioStep(
            label="initial-snapshot",
            message="What's going on with this patient right now?",
        ),
    ),
    think_time_s=DEFAULT_THINK_TIME_S,
)

#: UC-2 — a snapshot turn, then a follow-up in the same conversation.
UC2_FOLLOWUP = Scenario(
    name="UC-2-followup",
    steps=(
        ScenarioStep(
            label="initial-snapshot",
            message="Summarize this patient's current status.",
        ),
        ScenarioStep(
            label="followup",
            message="What about their most recent labs?",
            is_followup=True,
        ),
    ),
    think_time_s=DEFAULT_THINK_TIME_S,
)

SCENARIOS: tuple[Scenario, ...] = (UC1_SNAPSHOT, UC2_FOLLOWUP)
