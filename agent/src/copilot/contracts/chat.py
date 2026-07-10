"""``/chat`` endpoint request/response contracts (T011).

Frozen, strict (``extra="forbid"``) value objects so a malformed request —
missing field, wrong type, unknown field — fails Pydantic validation and
FastAPI's default handler turns that into a 422, never an unhandled 500.
"""

from __future__ import annotations

from enum import Enum

from pydantic import Field

from copilot.contracts.base import ContractModel
from copilot.contracts.tools import PatientSnapshotOutput
from copilot.contracts.verification import VerificationCounts


class DegradedReason(str, Enum):
    """Why a reply is degraded — the LLM never answered (T012).

    Backed (serialized to JSON) because it is the machine-readable marker a
    client reads to switch the panel into non-AI mode. Distinct from the T010
    ``FallbackReason`` axis: a response carries at most one of the two.
    """

    LLM_UNAVAILABLE = "llm_unavailable"


class ChatRequest(ContractModel):
    """One turn of a chat conversation.

    ``conversation_id`` omitted starts a new conversation; supplied, it must
    resolve to a conversation bound to this same ``patient_id`` and the same
    caller (by token hash) or the request is rejected (see the endpoint for
    the 404 scope-mismatch contract). ``token`` is the caller's bearer token,
    forwarded to tools and hashed for identity binding — never stored or
    echoed back raw.
    """

    message: str = Field(min_length=1)
    patient_id: str = Field(min_length=1)
    token: str = Field(min_length=1)
    conversation_id: str | None = None


class ChatResponse(ContractModel):
    """The non-streaming JSON shape of a ``/chat`` reply.

    ``reply`` is the verified answer text (or the fallback text — see
    ``fallback``). ``verification`` is the machine-readable T008/T009 verdict
    summary. ``correlation_id`` is echoed here in addition to the
    ``X-Correlation-ID`` response header (T001).
    """

    conversation_id: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)
    reply: str
    verification: VerificationCounts
    fallback: bool = False
    #: Set only when the LLM never answered (T012). ``None`` on the answer and
    #: on the T010-fallback paths — the two axes are mutually exclusive. Emitted
    #: only when non-``None`` (the endpoint serializes with ``exclude_none``).
    degraded: DegradedReason | None = None
    #: The raw T005 structured snapshot rendered straight from FHIR when the LLM
    #: is down on the first turn — no model involvement. ``None`` on every other
    #: path (follow-up outages carry no non-AI rendering).
    snapshot: PatientSnapshotOutput | None = None
