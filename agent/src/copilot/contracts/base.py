"""Shared base model for all contract types.

Contracts are the source of truth for tool I/O: every model is frozen
(immutable), forbids unknown fields, and validates on construction.
"""

from pydantic import BaseModel, ConfigDict


class ContractModel(BaseModel):
    """Base for all contract models: immutable, strict about extra fields."""

    model_config = ConfigDict(frozen=True, extra="forbid")
