"""The audit invocation record contract (T013).

ARCHITECTURE.md §7: the agent service POSTs one invocation record per
completed ``/chat`` turn to OpenEMR's audit-bridge endpoint (T016, out of
scope here), which writes it via ``EventAuditLogger`` alongside the EMR's
own access log — the missing decision/disclosure log.

PHI exclusion is enforced by the type, not by discipline: every field is an
identifier, a bounded enum, an int count, or an aware timestamp. There is no
free-text field. ``user_token_hash`` is the T011 ``sha256(token)`` digest —
the raw bearer token is never accepted here (or anywhere downstream of
:func:`copilot.conversation.hash_token`).
"""

from __future__ import annotations

from enum import Enum

from pydantic import AwareDatetime, Field

from copilot.agent.loop import FallbackReason
from copilot.contracts.base import ContractModel
from copilot.contracts.chat import DegradedReason


class AuditOutcome(str, Enum):
    """Which of the three completed-turn shapes produced this record.

    ``ANSWERED`` covers a normal verified reply *and* the verification-level
    fallback (all claims stripped, ``VerificationVerdict.fallback_triggered``)
    — both are turns the loop actually answered; the distinction lives in the
    ``claims_*`` counts, not a separate outcome. ``FALLBACK`` is the T010
    model-outcome axis (refusal, malformed output, etc. — see
    ``fallback_reason``). ``DEGRADED`` is the T012 non-AI axis (the LLM never
    answered — see ``degraded``). The three are mutually exclusive.
    """

    ANSWERED = "answered"
    FALLBACK = "fallback"
    DEGRADED = "degraded"


class AuditInvocationRecord(ContractModel):
    """One invocation's audit-bridge payload — PHI-free by construction.

    ``extra="forbid"`` (inherited from :class:`ContractModel`) plus a closed
    field set is what makes the PHI exclusion structural: no question text,
    no response text, no resource field values can be added to this model
    without a schema change reviewers will see, unlike a dict that silently
    grows a new key.
    """

    user_token_hash: str = Field(min_length=1)
    patient_id: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)
    conversation_id: str = Field(min_length=1)
    occurred_at: AwareDatetime
    claims_total: int = Field(ge=0)
    claims_passed: int = Field(ge=0)
    claims_stripped: int = Field(ge=0)
    outcome: AuditOutcome
    #: Set only when ``outcome`` is ``DEGRADED``; ``None`` otherwise.
    degraded: DegradedReason | None = None
    #: Set only when ``outcome`` is ``FALLBACK``; ``None`` otherwise.
    fallback_reason: FallbackReason | None = None
