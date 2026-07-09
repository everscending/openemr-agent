"""Verification-layer contracts (T008).

Frozen value objects modelling the *result* of Layer-1 source attribution
(ARCHITECTURE.md §5): the verified output text, a per-sentence verdict, the
machine-readable list of stripped claims, the observability counts, and the
``ResourceRef``s that were available to cite. The verifier itself is a pure,
deterministic, non-LLM function in :mod:`copilot.verification`.
"""

from enum import Enum

from pydantic import Field

from copilot.contracts.base import ContractModel
from copilot.contracts.refs import ResourceRef


class ClaimStatus(str, Enum):
    """Per-sentence verification outcome.

    ``verified`` and ``scaffolding`` survive into the output; every
    ``stripped_*`` outcome is removed and annotated. The failure kind is kept
    distinct so observability can tell an unknown citation from a type mismatch
    from a wholly uncited claim.
    """

    VERIFIED = "verified"
    SCAFFOLDING = "scaffolding"
    STRIPPED_UNCITED = "stripped_uncited"
    STRIPPED_UNKNOWN_CITATION = "stripped_unknown_citation"
    STRIPPED_TYPE_MISMATCH = "stripped_type_mismatch"


class ClaimVerdict(ContractModel):
    """One sentence's outcome: its status, its text, and the reason/ref."""

    status: ClaimStatus
    text: str = Field(min_length=1)
    reason: str | None = None
    ref: ResourceRef | None = None


class StrippedClaim(ContractModel):
    """A removed claim, machine-readable, with the reason it failed closed."""

    text: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    status: ClaimStatus


class VerificationCounts(ContractModel):
    """Observability counts over claim-bearing sentences (scaffolding excluded).

    ``claims_total`` always equals ``claims_passed + claims_stripped`` and is
    reported even when the response falls back entirely.
    """

    claims_total: int = Field(ge=0)
    claims_passed: int = Field(ge=0)
    claims_stripped: int = Field(ge=0)


class VerificationVerdict(ContractModel):
    """The full result of verifying one draft response.

    ``output_text`` is what to render: surviving sentences (with the removal
    marker appended when anything was stripped), or the fallback text when no
    claim-bearing sentence survives. ``available_refs`` carries the deep-linkable
    refs on every path — the stripped/partial path as well as the fallback path.
    """

    output_text: str
    verdicts: tuple[ClaimVerdict, ...] = ()
    stripped: tuple[StrippedClaim, ...] = ()
    counts: VerificationCounts
    available_refs: tuple[ResourceRef, ...] = ()
    content_removed: bool = False
    fallback_triggered: bool = False
