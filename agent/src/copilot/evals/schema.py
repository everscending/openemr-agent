"""Eval-case fixture schema (T015).

ARCHITECTURE.md section 8: eval cases live as versioned fixtures in the repo
(the platform is only a runner/viewer). Every case declares what it guards
against (a boundary, an invariant, or a regression), a deterministic scripted
scenario (no live LLM, no network), and the named checks that must hold over
the runner's observed behavior.

These are frozen, strict (``extra="forbid"``) Pydantic v2 models: a malformed
fixture fails at load time with a named error (see :mod:`copilot.evals.loader`),
never mid-run.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import Field

from copilot.contracts.base import ContractModel


class GuardsAgainst(str, Enum):
    """What class of risk one eval case exercises (ARCHITECTURE.md section 8)."""

    BOUNDARY = "boundary"
    INVARIANT = "invariant"
    REGRESSION = "regression"


class CheckRef(ContractModel):
    """One named, deterministic check to evaluate against the run's response.

    ``name`` must resolve in the check registry (see
    :mod:`copilot.evals.checks`) — an unregistered name is a load-time error,
    never a silent skip. ``params`` carries whatever the named check needs
    (e.g. ``category``, ``expected_status``, ``contains``) — deliberately a
    free-form mapping so the schema does not have to grow a field per check.
    """

    name: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)


class ScriptedToolCall(ContractModel):
    """One tool call the scripted LLM asks the loop to run."""

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


class ScriptedResponse(ContractModel):
    """One scripted LLM turn — the wire-neutral shape of ``ports.LLMResponse``.

    The runner converts these, in order, into the fixed script an in-process
    scripted fake LLM (implementing T010's ``LLMClient`` port) returns. There
    is never a live model call — determinism is the point (ARCHITECTURE.md
    section 8 / the eval harness design decisions).
    """

    stop_reason: Literal["tool_use", "end_turn", "refusal", "max_tokens"]
    text: str | None = None
    tool_calls: tuple[ScriptedToolCall, ...] = ()


class EvalScenario(ContractModel):
    """The deterministic scenario one case runs: fixture data + a script.

    ``fhir_fixture`` maps a FHIR resource type (e.g. ``"Patient"``,
    ``"MedicationRequest"``) to the list of resource JSON bodies the mock FHIR
    transport serves for that type — never a live network call.
    ``fhir_failures`` names resource types whose every request the mock
    transport answers with an upstream failure (500), for tool-failure cases.
    ``messages`` is the ordered list of user turns posted to ``/chat``;
    ``llm_script`` is the ordered, flat queue of LLM turns returned across the
    *whole* run (spanning every internal tool-use step of every message).
    """

    patient_id: str = Field(min_length=1)
    fhir_fixture: dict[str, tuple[dict[str, Any], ...]] = Field(default_factory=dict)
    fhir_failures: tuple[str, ...] = ()
    messages: tuple[str, ...] = Field(min_length=1)
    llm_script: tuple[ScriptedResponse, ...] = Field(min_length=1)


class EvalCase(ContractModel):
    """One eval-case fixture: id, what it guards against, scenario, checks.

    ``failure_mode`` documents the failure this case guards against (PRD
    engineering requirement) — required and non-empty; the loader rejects a
    case missing it or carrying an empty string (see
    :mod:`copilot.evals.loader`).
    """

    id: str = Field(min_length=1)
    guards_against: GuardsAgainst
    failure_mode: str = Field(min_length=1)
    scenario: EvalScenario
    checks: tuple[CheckRef, ...] = Field(min_length=1)
