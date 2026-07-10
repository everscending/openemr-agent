"""``/chat`` endpoint request/response contracts (T011).

Frozen, strict (``extra="forbid"``) value objects so a malformed request —
missing field, wrong type, unknown field — fails Pydantic validation and
FastAPI's default handler turns that into a 422, never an unhandled 500.
"""

from __future__ import annotations

from pydantic import Field

from copilot.contracts.base import ContractModel
from copilot.contracts.verification import VerificationCounts


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
