"""Token-cost computation from a configurable price table (T014 criterion 2).

Exact ``Decimal`` arithmetic — never ``float`` (float would silently round a
per-token rate and drift over millions of calls). When the LLM reports no
usage at all, or the model has no price-table entry, callers must treat cost
as *absent*, never zero — a zero would read as "free" on a dashboard, an
absent attribute reads as "unknown," which is the truth in both cases.

Pure module: no OpenTelemetry import of any kind.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Mapping

from pydantic import Field

from copilot.contracts.base import ContractModel

_PER_MILLION = Decimal(1_000_000)


class ModelPricing(ContractModel):
    """Per-million-token input/output rates for one model identifier."""

    input_rate_per_million: Decimal = Field(ge=0)
    output_rate_per_million: Decimal = Field(ge=0)


PriceTable = Mapping[str, ModelPricing]

#: A conservative default table covering this service's own default chat
#: model. Production deployments override via the ``price_table``
#: constructor seam on :class:`~copilot.agent.loop.AgentLoop` — this is a
#: fallback, not a pricing authority.
DEFAULT_PRICE_TABLE: PriceTable = {
    "claude-opus-4-8": ModelPricing(
        input_rate_per_million=Decimal("15"),
        output_rate_per_million=Decimal("75"),
    ),
}


def compute_cost(
    model: str,
    input_tokens: int | None,
    output_tokens: int | None,
    price_table: PriceTable,
) -> Decimal | None:
    """The exact cost of one LLM turn, or ``None`` when it cannot be computed.

    Returns ``None`` (never ``Decimal("0")``) when:

    - the LLM reported no token usage at all (``input_tokens`` and
      ``output_tokens`` both ``None``), or
    - ``model`` has no entry in ``price_table`` (an unpriced/unconfigured
      model must never be silently reported as free).
    """
    if input_tokens is None and output_tokens is None:
        return None
    pricing = price_table.get(model)
    if pricing is None:
        return None
    input_cost = Decimal(input_tokens or 0) * pricing.input_rate_per_million
    output_cost = Decimal(output_tokens or 0) * pricing.output_rate_per_million
    return (input_cost + output_cost) / _PER_MILLION
