"""Conversation state store — Redis-shaped ``get``/``put``/``expire`` (T011).

ARCHITECTURE.md §3: conversations are scoped to (user, patient, session); v1
holds state in-process, but the interface is Redis-shaped so horizontal
scaling is a configuration change, not a redesign. §7: conversation state is
ephemeral — a TTL of hours, never persisted.

A :class:`ConversationRecord` binds a conversation to exactly one patient and
one caller identity at creation. The caller identity is the SHA-256 hex
digest of the presented bearer token (:func:`hash_token`) — the raw token is
never stored, matching the ``/chat`` endpoint's contract that the token
string must appear in no stored record.

:class:`InMemoryConversationStore` is the only implementation in v1. It
enforces a configurable TTL against an **injected clock**, never wall-clock
time read directly, so expiry is deterministically testable. An expired
conversation is deleted on read (not merely hidden behind a time check): a
second read after expiry cannot resurrect it, and ``__len__`` reflects the
deletion for callers that want to observe store hygiene directly.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Callable, Protocol

from pydantic import Field

from copilot.contracts.base import ContractModel

#: ARCHITECTURE.md:403 — "a TTL of hours." Two hours is the default; callers
#: may configure a different value (see ``InMemoryConversationStore``).
DEFAULT_TTL_SECONDS: float = 2 * 60 * 60


def hash_token(token: str) -> str:
    """SHA-256 hex digest of a bearer token — never store or log the token itself."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class TurnRole(str, Enum):
    """Who spoke a conversation turn. Backed (persisted/serialized) — Redis-shaped."""

    USER = "user"
    ASSISTANT = "assistant"


class ConversationTurn(ContractModel):
    """One replayed turn of a conversation's history."""

    role: TurnRole
    content: str


class ConversationRecord(ContractModel):
    """A conversation's bound scope plus its ordered turns.

    ``patient_id`` and ``user_token_hash`` are set at creation and compared on
    every subsequent turn (the scope-binding check lives in the ``/chat``
    endpoint, not here) — conversation scoping is patient binding wearing a
    different hat, the same class of boundary the agent loop enforces one
    layer down for tool calls.
    """

    patient_id: str = Field(min_length=1)
    user_token_hash: str = Field(min_length=1)
    turns: tuple[ConversationTurn, ...] = ()

    def with_turn(self, role: TurnRole, content: str) -> "ConversationRecord":
        """Return a new record with one more turn appended (immutable wither)."""
        return self.model_copy(
            update={"turns": self.turns + (ConversationTurn(role=role, content=content),)}
        )


class ConversationStore(Protocol):
    """The Redis-shaped state interface the ``/chat`` endpoint depends on.

    Narrow by design: ``get``/``put``/``expire`` map directly onto Redis's
    ``GET``/``SET .. EX``/``DEL`` semantics, so a future Redis-backed
    implementation is a swap behind this protocol, not a redesign.
    """

    def get(self, conversation_id: str) -> ConversationRecord | None: ...

    def put(self, conversation_id: str, record: ConversationRecord) -> None: ...

    def expire(self, conversation_id: str) -> None: ...


def _default_now() -> datetime:
    return datetime.now(timezone.utc)


class InMemoryConversationStore:
    """Single-process ``ConversationStore`` enforcing a configurable TTL.

    ``now`` is an injected clock (PSR-20-style discipline): the store never
    reads wall-clock time directly, so TTL expiry is deterministically
    testable. TTL is sliding — every ``put`` (including the update after each
    conversation turn) resets the expiry to ``now() + ttl_seconds``.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        now: Callable[[], datetime] = _default_now,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._ttl_seconds = ttl_seconds
        self._now = now
        self._entries: dict[str, tuple[ConversationRecord, datetime]] = {}

    def get(self, conversation_id: str) -> ConversationRecord | None:
        """Return the record, or ``None`` if unknown or expired.

        An expired entry is deleted here, in the read path — not merely
        filtered — so a second read cannot resurrect it and the store does
        not accumulate ghost conversations.
        """
        entry = self._entries.get(conversation_id)
        if entry is None:
            return None
        record, expires_at = entry
        if self._now() >= expires_at:
            self.expire(conversation_id)
            return None
        return record

    def put(self, conversation_id: str, record: ConversationRecord) -> None:
        """Store ``record``, (re)setting its expiry to ``now() + ttl_seconds``."""
        expires_at = self._now() + timedelta(seconds=self._ttl_seconds)
        self._entries[conversation_id] = (record, expires_at)

    def expire(self, conversation_id: str) -> None:
        """Immediately delete an entry, regardless of its remaining TTL."""
        self._entries.pop(conversation_id, None)

    def __len__(self) -> int:
        """Number of entries currently held (Redis ``DBSIZE`` analog)."""
        return len(self._entries)
