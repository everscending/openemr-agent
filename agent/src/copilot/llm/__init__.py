"""LLM adapters implementing the agent loop's :class:`LLMClient` port.

Exactly one thin adapter imports the vendor SDK (``anthropic``). The loop
imports the port, never this package, so importing the loop pulls in no SDK.
"""
