"""Tests for T043 — env-configurable default chat model.

Criteria map (see the T043 ticket):
  1.  No ``CHAT_MODEL`` set: ``create_app()`` resolves the chat model to
      ``"claude-sonnet-5"`` for *both* the constructed LLM client and the
      value passed to ``AgentLoop``.
  2.  ``CHAT_MODEL=<value>`` set, no explicit ``chat_model`` argument: the
      resolved model is ``<value>`` for both the LLM client and the
      ``AgentLoop`` model — never one without the other.
  3.  An explicit ``chat_model=`` argument to ``create_app()`` still
      overrides the environment.
  4.  ``DEFAULT_PRICE_TABLE["claude-sonnet-5"]`` exists and ``compute_cost``
      returns a ``Decimal`` for it, not ``None``.
  5.  ``AnthropicLLMClient`` construction and tests that pass their own
      explicit model string are unaffected (covered by not modifying those
      test files at all — this module never touches them).

Mandatory adversarial test: a third env value (neither the old nor the new
hardcoded default) must reach *both* the LLM client and the AgentLoop/
telemetry-visible model string identically — this is the exact
desynchronization the ticket's design decision 4 exists to prevent.

Production code (``copilot.app``'s new ``CHAT_MODEL_ENV``/``default_chat_model``
seam) is referenced lazily inside test bodies where genuinely new, so
collection succeeds before the implementation exists (RED = the missing
feature per test).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from copilot.agent import ports
from copilot.agent.tools import ToolRegistry
from copilot.app import CHAT_MODEL_ENV, DEFAULT_CHAT_MODEL, create_app, default_chat_model

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def final(text: str, *, input_tokens: int = 10, output_tokens: int = 5) -> Any:
    return ports.LLMResponse(
        stop_reason=ports.StopReason.END_TURN,
        text=text,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def build_tracer(exporter: InMemorySpanExporter) -> Any:
    """A private ``TracerProvider`` per test — never the global provider, so
    exported spans from one test can never leak into another."""
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("copilot-tests")


def span_by_name(exporter: InMemorySpanExporter, name: str) -> Any:
    return next(s for s in exporter.get_finished_spans() if s.name == name)


def chat_body(
    message: str = "Catch me up.",
    *,
    patient_id: str = "pat-1",
    token: str = "user-token-abc",
) -> dict[str, Any]:
    return {"message": message, "patient_id": patient_id, "token": token}


def install_recording_llm_client(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stands in for ``AnthropicLLMClient`` (via the module attribute
    ``_default_chat_llm`` lazily imports) so we can observe exactly which
    model string the *production default-LLM-client seam* was constructed
    with — without opening any network connection. Returns the list of model
    strings every construction was called with, in order.
    """
    constructed_models: list[str] = []

    class RecordingLLMClient:
        def __init__(self, *, model: str) -> None:
            self.model = model
            constructed_models.append(model)

        async def complete(self, *, system: str, messages: Any, tools: Any) -> Any:
            return final("Reviewed the chart.")

    monkeypatch.setattr(
        "copilot.llm.anthropic_client.AnthropicLLMClient", RecordingLLMClient
    )
    return constructed_models


def make_app_with_recording_llm(
    monkeypatch: pytest.MonkeyPatch,
    exporter: InMemorySpanExporter,
    *,
    chat_model: str | None = None,
) -> tuple[TestClient, list[str]]:
    """Build a real app through the *default* LLM-client seam (no
    ``chat_llm`` injection) so the resolution path under test — the one
    ``create_app`` actually uses in production — runs for real.
    """
    constructed_models = install_recording_llm_client(monkeypatch)
    tracer = build_tracer(exporter)
    kwargs: dict[str, Any] = {
        "chat_registry_factory": lambda token: ToolRegistry(()),
        "tracer": tracer,
    }
    if chat_model is not None:
        kwargs["chat_model"] = chat_model
    app = create_app(**kwargs)
    return TestClient(app), constructed_models


# ---------------------------------------------------------------------------
# Constants / pure resolver (criteria 1-3, pinned design decisions 1-3)
# ---------------------------------------------------------------------------


def test_chat_model_env_var_name_is_chat_model() -> None:
    assert CHAT_MODEL_ENV == "CHAT_MODEL"


def test_default_chat_model_constant_is_sonnet_5() -> None:
    assert DEFAULT_CHAT_MODEL == "claude-sonnet-5"


def test_default_chat_model_resolver_falls_back_when_env_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(CHAT_MODEL_ENV, raising=False)
    assert default_chat_model() == "claude-sonnet-5"


def test_default_chat_model_resolver_reads_env_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(CHAT_MODEL_ENV, "claude-haiku-9000")
    assert default_chat_model() == "claude-haiku-9000"


# ---------------------------------------------------------------------------
# Criterion 1 — no env set: both the LLM client and AgentLoop resolve to the
# new hardcoded default.
# ---------------------------------------------------------------------------


def test_no_env_set_resolves_sonnet_5_for_llm_client_and_agent_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(CHAT_MODEL_ENV, raising=False)
    exporter = InMemorySpanExporter()
    client, constructed_models = make_app_with_recording_llm(monkeypatch, exporter)

    resp = client.post("/chat", json=chat_body())
    assert resp.status_code == 200

    # The LLM client actually built used the new default.
    assert constructed_models == ["claude-sonnet-5"]
    # The model value visible to AgentLoop/telemetry (the llm.call span's
    # "model" attribute is set from AgentLoop's own `self._model`) is the
    # *same* string — not independently defaulted.
    llm_span = span_by_name(exporter, "llm.call")
    assert llm_span.attributes["model"] == "claude-sonnet-5"


# ---------------------------------------------------------------------------
# Criterion 2 — CHAT_MODEL set, no explicit chat_model arg: both paths read
# the same env-resolved value.
# ---------------------------------------------------------------------------


def test_env_set_with_no_explicit_arg_resolves_for_both_llm_client_and_agent_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(CHAT_MODEL_ENV, "claude-sonnet-5")
    exporter = InMemorySpanExporter()
    client, constructed_models = make_app_with_recording_llm(monkeypatch, exporter)

    resp = client.post("/chat", json=chat_body())
    assert resp.status_code == 200

    assert constructed_models == ["claude-sonnet-5"]
    llm_span = span_by_name(exporter, "llm.call")
    assert llm_span.attributes["model"] == "claude-sonnet-5"


# ---------------------------------------------------------------------------
# Mandatory adversarial test — a THIRD value, neither the old nor the new
# hardcoded default, must reach both paths identically. This is the exact
# regression design decision 4 exists to prevent: env-configuring only one
# side would silently desynchronize "the model actually called" from "the
# model name recorded for telemetry/cost lookups," and a value equal to
# either hardcoded default could not expose that bug.
# ---------------------------------------------------------------------------


def test_third_env_value_propagates_identically_to_llm_client_and_agent_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decoy_model = "claude-nimbus-3-preview"
    assert decoy_model not in ("claude-sonnet-5", "claude-opus-4-8")
    monkeypatch.setenv(CHAT_MODEL_ENV, decoy_model)
    exporter = InMemorySpanExporter()
    client, constructed_models = make_app_with_recording_llm(monkeypatch, exporter)

    resp = client.post("/chat", json=chat_body())
    assert resp.status_code == 200

    # Neither path silently fell back to either hardcoded string.
    assert constructed_models == [decoy_model]
    llm_span = span_by_name(exporter, "llm.call")
    assert llm_span.attributes["model"] == decoy_model
    assert llm_span.attributes["model"] == constructed_models[0]


# ---------------------------------------------------------------------------
# Criterion 3 — an explicit chat_model= argument overrides the environment.
# ---------------------------------------------------------------------------


def test_explicit_chat_model_argument_overrides_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Env is set to something the caller's explicit argument must NOT produce.
    monkeypatch.setenv(CHAT_MODEL_ENV, "claude-should-not-be-used")
    exporter = InMemorySpanExporter()
    client, constructed_models = make_app_with_recording_llm(
        monkeypatch, exporter, chat_model="claude-explicit-override"
    )

    resp = client.post("/chat", json=chat_body())
    assert resp.status_code == 200

    assert constructed_models == ["claude-explicit-override"]
    llm_span = span_by_name(exporter, "llm.call")
    assert llm_span.attributes["model"] == "claude-explicit-override"


def test_explicit_chat_model_argument_overrides_absent_environment_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(CHAT_MODEL_ENV, raising=False)
    exporter = InMemorySpanExporter()
    client, constructed_models = make_app_with_recording_llm(
        monkeypatch, exporter, chat_model="claude-explicit-override-2"
    )

    resp = client.post("/chat", json=chat_body())
    assert resp.status_code == 200

    assert constructed_models == ["claude-explicit-override-2"]
    llm_span = span_by_name(exporter, "llm.call")
    assert llm_span.attributes["model"] == "claude-explicit-override-2"


# ---------------------------------------------------------------------------
# `_default_chat_llm` accepts the resolved model as a parameter (design
# decision 4), rather than independently reading the module constant.
# ---------------------------------------------------------------------------


def test_default_chat_llm_accepts_model_parameter_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from copilot.app import _default_chat_llm

    constructed_models = install_recording_llm_client(monkeypatch)

    llm = _default_chat_llm("claude-some-injected-model")

    assert constructed_models == ["claude-some-injected-model"]
    assert llm.model == "claude-some-injected-model"


# ---------------------------------------------------------------------------
# Criterion 4 — pricing table has an entry for the new default model.
# ---------------------------------------------------------------------------


def test_pricing_table_has_sonnet_5_entry_and_computes_a_real_cost() -> None:
    from copilot.telemetry.pricing import DEFAULT_PRICE_TABLE, compute_cost

    assert "claude-sonnet-5" in DEFAULT_PRICE_TABLE

    cost = compute_cost("claude-sonnet-5", 1000, 500, DEFAULT_PRICE_TABLE)

    assert isinstance(cost, Decimal)
    # (1000 * 3 + 500 * 15) / 1_000_000 == 0.0105, exact Decimal arithmetic.
    assert cost == Decimal("0.0105")


def test_pricing_table_still_has_the_existing_opus_entry_unchanged() -> None:
    """My own adversarial probe: adding the sonnet row must not disturb or
    replace the existing opus pricing — this table accretes, it doesn't
    swap."""
    from copilot.telemetry.pricing import DEFAULT_PRICE_TABLE, compute_cost

    assert "claude-opus-4-8" in DEFAULT_PRICE_TABLE
    cost = compute_cost("claude-opus-4-8", 1000, 500, DEFAULT_PRICE_TABLE)
    assert cost == Decimal("0.0525")


def test_pricing_table_unpriced_model_still_returns_none() -> None:
    """My own adversarial probe: the new entry must not make compute_cost
    permissive for arbitrary strings — an unpriced model is still absent,
    never silently priced at zero or at the new default's rate."""
    from copilot.telemetry.pricing import DEFAULT_PRICE_TABLE, compute_cost

    assert compute_cost("claude-totally-unpriced", 1000, 500, DEFAULT_PRICE_TABLE) is None
