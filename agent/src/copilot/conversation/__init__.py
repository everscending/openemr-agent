"""Conversation state (T011): scoped, ephemeral, Redis-shaped storage."""

from copilot.conversation.store import (
    ConversationRecord,
    ConversationStore,
    ConversationTurn,
    DEFAULT_TTL_SECONDS,
    InMemoryConversationStore,
    TurnRole,
    hash_token,
)

__all__ = [
    "ConversationRecord",
    "ConversationStore",
    "ConversationTurn",
    "DEFAULT_TTL_SECONDS",
    "InMemoryConversationStore",
    "TurnRole",
    "hash_token",
]
