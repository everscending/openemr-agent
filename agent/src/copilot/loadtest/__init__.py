"""Load/stress-test harness (T030).

Drives the agent's ``/chat`` endpoint with realistic snapshot (UC-1) and
follow-up (UC-2) scenarios at a target concurrency, recording p50/p95/p99
latency and error rate, and stops a level immediately via
:class:`~copilot.loadtest.abort_guard.AbortGuard` if it starts hammering a
rate-limited or failing deployment. This package is the harness itself —
the *recorded numbers* from real runs live in ``docs/perf/``, not here.
"""

from __future__ import annotations

from copilot.loadtest.abort_guard import AbortDecision, AbortGuard
from copilot.loadtest.results import (
    LoadTestStats,
    RequestResult,
    ResultsParseError,
    load_results_ndjson,
    parse_results,
    stats_to_dict,
)
from copilot.loadtest.runner import (
    LevelReport,
    UserContext,
    compute_ramp_delays,
    run_level,
    run_virtual_user,
)
from copilot.loadtest.scenarios import SCENARIOS, UC1_SNAPSHOT, UC2_FOLLOWUP, Scenario, ScenarioStep

__all__ = [
    "AbortDecision",
    "AbortGuard",
    "LevelReport",
    "LoadTestStats",
    "RequestResult",
    "ResultsParseError",
    "SCENARIOS",
    "Scenario",
    "ScenarioStep",
    "UC1_SNAPSHOT",
    "UC2_FOLLOWUP",
    "UserContext",
    "compute_ramp_delays",
    "load_results_ndjson",
    "parse_results",
    "run_level",
    "run_virtual_user",
    "stats_to_dict",
]
