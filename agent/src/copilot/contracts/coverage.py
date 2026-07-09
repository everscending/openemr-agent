"""Per-category coverage reporting.

Responses must state what was and wasn't checked (ARCHITECTURE.md
section 7). Each data category resolves to exactly one of three
outcomes, modeled as a discriminated union so that an ``unavailable``
entry can never carry records.
"""

from typing import Annotated, Literal, Union

from pydantic import AwareDatetime, Field

from copilot.contracts.base import ContractModel


class CoverageOk(ContractModel):
    """Category was fetched successfully and returned records."""

    status: Literal["ok"] = "ok"
    category: str = Field(min_length=1)
    record_count: int = Field(ge=0)


class CoverageVerifiedEmpty(ContractModel):
    """Category was fetched successfully and is verifiably empty.

    Carries a query receipt: what was searched, its scope, and when.
    """

    status: Literal["verified_empty"] = "verified_empty"
    category: str = Field(min_length=1)
    query_description: str = Field(min_length=1)
    scope: str = Field(min_length=1)
    timestamp: AwareDatetime


class CoverageUnavailable(ContractModel):
    """Category could not be fetched. Never carries records."""

    status: Literal["unavailable"] = "unavailable"
    category: str = Field(min_length=1)
    reason: str = Field(min_length=1)


CategoryCoverage = Annotated[
    Union[CoverageOk, CoverageVerifiedEmpty, CoverageUnavailable],
    Field(discriminator="status"),
]
