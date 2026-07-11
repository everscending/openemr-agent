"""Tests for T041 — greeting / non-clinical conversational handling.

The defect (reproduced live): a bare greeting ("Hello!") returns the T008
verification fallback because the static ``SYSTEM_PROMPT`` frames the agent
purely as a record-synthesizer and gives no guidance for conversational /
non-clinical input, so the LLM emits an unsolicited (ungrounded) patient
summary that the deterministic verifier correctly strips.

The fix is a **static** additive clause to ``SYSTEM_PROMPT`` telling the agent
to answer conversational / non-clinical messages briefly and directly, without
an unsolicited patient summary and without fabricating patient claims or
citations — while leaving the citation contract, the refusal boundary, and the
entire verification layer unchanged.

Criteria map (see .tdd-swarm/tickets/T041-greeting-conversational-handling.md):
  1. Prompt carries non-clinical/conversational guidance (asserted on the text).
  2. Citation contract + refusal boundary remain present and unweakened.
  3. Prompt stays static: no patient id / date / interpolation token.
  4. Pipeline behavior via injected ``ScriptedLLM``:
       (a) a scripted greeting with no citations flows through the loop and is
           returned VERBATIM — not stripped, not the fallback.
       (b) a scripted uncited patient claim is STILL stripped (verifier backstop
           intact; the change opened no loophole).
  5. Scope: loop.py SYSTEM_PROMPT + this test file only.

Production code is imported lazily inside bodies so collection succeeds before
any change lands (RED = the missing guidance / behavior per test). The
``ScriptedLLM`` fake and the ``final`` builder are reused from the T010 suite.
"""

from __future__ import annotations

import re
from typing import Any

from test_agent_loop import ScriptedLLM, build_loop, final, make_registry


def loop_mod() -> Any:
    from copilot.agent import loop

    return loop


# ==========================================================================
# Criterion 1 — the prompt carries non-clinical / conversational guidance
# ==========================================================================


def test_system_prompt_carries_conversational_guidance() -> None:
    lowered = loop_mod().SYSTEM_PROMPT.lower()
    # Distinctive, stable wording added by this ticket.
    assert "conversational" in lowered
    assert "non-clinical" in lowered
    # It answers such input directly, NOT with an unsolicited summary.
    assert "briefly and directly" in lowered
    assert "unsolicited patient summary" in lowered


def test_conversational_guidance_covers_greetings_and_capability_questions() -> None:
    lowered = loop_mod().SYSTEM_PROMPT.lower()
    # The three named non-clinical shapes: greeting, capability question,
    # acknowledgement (ticket criterion 1).
    assert "greeting" in lowered
    assert "acknowledg" in lowered  # acknowledgement / acknowledgment
    # A capability question ("what can you do?") is answered by describing
    # capabilities, not by summarizing the patient.
    assert "capabilit" in lowered  # capabilities / capability


# ==========================================================================
# Criterion 1 + 3 — the exception opens NO citation loophole (prompt text)
# ==========================================================================


def test_conversational_clause_does_not_sanction_uncited_patient_claims() -> None:
    lowered = loop_mod().SYSTEM_PROMPT.lower()
    # The "no citations needed" carve-out is scoped strictly to non-clinical
    # content; every statement ABOUT THE PATIENT still cites every resource.
    assert "still cites every resource" in lowered
    # It explicitly forbids fabricating patient claims / citations in the reply.
    assert "fabricate" in lowered


# ==========================================================================
# Criterion 2 — citation contract + refusal boundary still present, unweakened
# ==========================================================================


def test_citation_contract_survives_the_edit() -> None:
    prompt = loop_mod().SYSTEM_PROMPT
    lowered = prompt.lower()
    # Same substrings the LOCKED T010 citation-contract test asserts.
    assert "[ResourceType/id]" in prompt
    assert "every resource" in lowered
    assert "claim" in lowered


def test_refusal_boundary_survives_the_edit() -> None:
    lowered = loop_mod().SYSTEM_PROMPT.lower()
    # Same substrings the LOCKED T010 refusal-boundary test asserts.
    assert "does not practice medicine" in lowered or "not practice medicine" in lowered
    assert "other" in lowered and "patient" in lowered


# ==========================================================================
# Criterion 3 — prompt stays static: no id / date / interpolation token
# ==========================================================================


def test_prompt_has_no_dates_ids_or_interpolation() -> None:
    prompt = loop_mod().SYSTEM_PROMPT
    # No ISO date, no obvious patient-id token (mirrors the LOCKED T010 guard).
    assert re.search(r"\d{4}-\d{2}-\d{2}", prompt) is None
    assert "pat-" not in prompt.lower()
    # No runtime interpolation left behind: no f-string/.format braces or %-fmt.
    assert "{" not in prompt and "}" not in prompt
    assert "%s" not in prompt and "%d" not in prompt


# ==========================================================================
# Criterion 4(a) — a scripted greeting flows through and is returned VERBATIM
# ==========================================================================


GREETING = (
    "Hello! I can summarize this patient's medications, problems, recent "
    "visits, and more, so what would you like to know?"
)


async def test_greeting_flows_through_the_loop_verbatim() -> None:
    llm = ScriptedLLM([final(GREETING)])
    result = await build_loop(llm, make_registry()).run("Hello!")

    # Not the loop's structured fallback, not the verifier's fallback.
    assert not result.is_fallback
    assert result.verdict is not None
    assert not result.verdict.fallback_triggered
    # Returned verbatim — not stripped, no removal marker.
    assert result.output_text == GREETING
    assert result.verdict.counts.claims_stripped == 0
    assert result.verdict.counts.claims_passed == 0


async def test_second_greeting_variant_also_survives_verbatim() -> None:
    # Fix the rule, not the example: a different greeting shape must also pass.
    greeting = (
        "Hi there, I'm the clinical co-pilot for this chart and I can pull up "
        "labs, medications, and visits when you ask"
    )
    llm = ScriptedLLM([final(greeting)])
    result = await build_loop(llm, make_registry()).run("who are you?")

    assert not result.is_fallback
    assert result.verdict is not None
    assert not result.verdict.fallback_triggered
    assert result.output_text == greeting


# ==========================================================================
# Criterion 4(b) — an uncited patient claim is STILL stripped (no loophole)
# ==========================================================================


async def test_uncited_patient_claim_is_still_stripped() -> None:
    # The verifier backstop must remain intact regardless of the prompt nudge.
    llm = ScriptedLLM([final("The patient has type 2 diabetes.")])
    result = await build_loop(llm, make_registry()).run("Hello!")

    assert result.verdict is not None
    assert result.verdict.fallback_triggered
    assert "diabetes" not in result.output_text
    assert result.verdict.counts.claims_stripped == 1
    assert result.verdict.counts.claims_passed == 0


async def test_second_uncited_claim_variant_is_still_stripped() -> None:
    # A different uncited clinical claim shape must also fail closed.
    llm = ScriptedLLM([final("Her most recent hemoglobin was 13.2 g/dL.")])
    result = await build_loop(llm, make_registry()).run("thanks")

    assert result.verdict is not None
    assert result.verdict.fallback_triggered
    assert "hemoglobin" not in result.output_text
    assert result.verdict.counts.claims_passed == 0
