"""Verification-layer contracts (T008).

Frozen value objects modelling the *result* of Layer-1 source attribution
(ARCHITECTURE.md §5): the verified output text, a per-sentence verdict, the
machine-readable list of stripped claims, the observability counts, and the
``ResourceRef``s that were available to cite. The verifier itself is a pure,
deterministic, non-LLM function in :mod:`copilot.verification`.
"""

from decimal import Decimal
from enum import Enum

from pydantic import Field

from copilot.contracts.base import ContractModel
from copilot.contracts.refs import ResourceRef


class ClaimStatus(str, Enum):
    """Per-sentence verification outcome.

    ``verified`` and ``scaffolding`` survive into the output; every
    ``stripped_*`` outcome is removed and annotated. The failure kind is kept
    distinct so observability can tell an unknown citation from a type mismatch
    from a wholly uncited claim — and (T009) a fabricated *source* from a
    misstated *value*: ``stripped_numeric_mismatch`` and
    ``stripped_date_mismatch`` name a grounded citation whose numeric/date
    content contradicted the cited resource, which is a different failure from
    ``stripped_unknown_citation`` (the citation itself is real).
    """

    VERIFIED = "verified"
    SCAFFOLDING = "scaffolding"
    STRIPPED_UNCITED = "stripped_uncited"
    STRIPPED_UNKNOWN_CITATION = "stripped_unknown_citation"
    STRIPPED_TYPE_MISMATCH = "stripped_type_mismatch"
    STRIPPED_NUMERIC_MISMATCH = "stripped_numeric_mismatch"
    STRIPPED_DATE_MISMATCH = "stripped_date_mismatch"


class NumericCheck(str, Enum):
    """Outcome of the T009 content check for one grounding-verified claim.

    ``checked`` — a parseable quantity/date was compared and matched a cited
    resource. ``unchecked`` — no comparison was possible (no parseable quantity,
    or no cited resource carried a comparable field); a pass-through that
    survives into the output but is **never** reported as numerically verified.
    ``mismatch`` — a quantity/date contradicted the cited resource; the claim is
    stripped (its :class:`ClaimStatus` is ``stripped_numeric_mismatch`` or
    ``stripped_date_mismatch``).
    """

    CHECKED = "checked"
    UNCHECKED = "unchecked"
    MISMATCH = "mismatch"


class NumericFinding(ContractModel):
    """Detail of the numeric/date content check attached to a claim (T009).

    ``unit_checked`` is ``False`` when the matched value's unit was absent or
    unknown to the minimal conversion table (the value matched but the unit was
    not verified). ``resource_date_unknown`` is ``True`` when a cited resource's
    date was missing/``0000-00-00``/empty/unparseable and so was treated as
    "unknown" (§8 data-quality boundary), never parsed into a real date.
    ``claimed``/``actual`` name the contradicting values on a ``mismatch``.
    """

    check: NumericCheck
    unit_checked: bool = True
    resource_date_unknown: bool = False
    claimed: str | None = None
    actual: str | None = None


class ResourceFacts(ContractModel):
    """Structured, comparable fields of a cited resource, for content checks.

    The T009 checker compares a claim's parseable quantities/dates against these
    fields. ``value``/``unit`` model a FHIR ``valueQuantity``; ``date`` is the
    resource's raw ``effectiveDateTime``/``authoredOn`` text kept as a **string**
    so the checker can apply the §8 data-quality boundary defensively —
    ``0000-00-00``, empty, and unparseable dates are "unknown" and never parsed.
    """

    ref: ResourceRef
    value: Decimal | None = None
    unit: str | None = None
    date: str | None = None


class ClaimVerdict(ContractModel):
    """One sentence's outcome: its status, its text, and the reason/ref.

    ``numeric`` carries the T009 content-check detail on a grounding-verified
    claim (``None`` when the content check did not run — scaffolding or a
    grounding-stripped claim).
    """

    status: ClaimStatus
    text: str = Field(min_length=1)
    reason: str | None = None
    ref: ResourceRef | None = None
    numeric: NumericFinding | None = None


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
    #: Grounding-verified claims whose numeric/date content was compared and
    #: matched a cited resource (T009). Excludes ``unchecked`` claims — an
    #: unchecked claim is never counted as numerically verified.
    numeric_checked: int = Field(default=0, ge=0)
    #: Grounding-verified claims for which no content comparison was possible
    #: (no parseable quantity/date, or no cited resource carried a comparable
    #: field). Mirrors T007's ``unavailable_uncoded``: never a silent clean pass.
    numeric_unchecked: int = Field(default=0, ge=0)


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
