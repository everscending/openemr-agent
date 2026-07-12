"""Tests for the virtual-user / level runner (T030 criteria 1, 3, 5).

Two properties are asserted at the transport seam (never via a return
value — see CLAUDE.md's "assert the property, not its proxy"):

  * a UC-2 follow-up step carries the SAME token string and the
    conversation_id from the prior turn's response — the T040 dependency
    this ticket's auth-seam note calls out by name.
  * the mandatory abort guard, wired per-result, actually stops a level
    early — fewer total requests than a full (unaborted) run would produce.

``asyncio_mode = auto`` (pyproject.toml) — no explicit marker needed on the
async tests below.
"""

from __future__ import annotations

import asyncio
import json
from typing import Callable

import httpx

from copilot.loadtest.abort_guard import AbortGuard
from copilot.loadtest.runner import (
    UserContext,
    compute_ramp_delays,
    run_level,
    run_virtual_user,
)
from copilot.loadtest.scenarios import UC1_SNAPSHOT, UC2_FOLLOWUP


class SleepSpy:
    """Records every requested sleep duration; never actually sleeps."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _fake_clock() -> Callable[[], float]:
    state = {"t": 0.0}

    def clock() -> float:
        state["t"] += 1.0
        return state["t"]

    return clock


def _make_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# run_virtual_user — UC-1 (single turn, no conversation)
# ---------------------------------------------------------------------------


async def test_uc1_sends_a_single_request_with_no_conversation_id() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"conversation_id": "conv-1", "reply": "ok"})

    async with _make_client(handler) as client:
        results = await run_virtual_user(
            client=client,
            base_url="http://agent",
            scenario=UC1_SNAPSHOT,
            user=UserContext(patient_id="p-1", token="tok-1"),
            clock=_fake_clock(),
            sleep=SleepSpy(),
        )

    assert len(captured) == 1
    payload = json.loads(captured[0].read())
    assert payload["patient_id"] == "p-1"
    assert payload["token"] == "tok-1"
    assert "conversation_id" not in payload
    assert len(results) == 1
    assert results[0].status_code == 200
    assert results[0].error is None


# ---------------------------------------------------------------------------
# run_virtual_user — UC-2 (follow-up reuses token + conversation_id)
# ---------------------------------------------------------------------------


async def test_uc2_followup_reuses_same_token_and_prior_conversation_id() -> None:
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read())
        captured.append(payload)
        return httpx.Response(
            200, json={"conversation_id": "conv-xyz", "reply": "ok"}
        )

    sleep_spy = SleepSpy()
    async with _make_client(handler) as client:
        await run_virtual_user(
            client=client,
            base_url="http://agent",
            scenario=UC2_FOLLOWUP.with_think_time(17.5),
            user=UserContext(patient_id="p-2", token="tok-STABLE"),
            clock=_fake_clock(),
            sleep=sleep_spy,
        )

    assert len(captured) == 2
    turn1, turn2 = captured
    assert "conversation_id" not in turn1

    # The transport-seam assertion: turn2's token is byte-identical to
    # turn1's — not merely "both present" — and its conversation_id is
    # exactly the one turn1's response returned.
    assert turn2["token"] == turn1["token"] == "tok-STABLE"
    assert turn2["conversation_id"] == "conv-xyz"

    # Documented think-time actually applied between the two turns.
    assert sleep_spy.calls == [17.5]


async def test_uc2_omits_conversation_id_on_followup_when_first_turn_failed() -> None:
    call_count = {"n": 0}
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        captured.append(json.loads(request.read()))
        if call_count["n"] == 1:
            return httpx.Response(500, json={"detail": "boom"})
        return httpx.Response(200, json={"conversation_id": "conv-late", "reply": "ok"})

    async with _make_client(handler) as client:
        results = await run_virtual_user(
            client=client,
            base_url="http://agent",
            scenario=UC2_FOLLOWUP.with_think_time(0.0),
            user=UserContext(patient_id="p-3", token="tok-3"),
            clock=_fake_clock(),
            sleep=SleepSpy(),
        )

    assert "conversation_id" not in captured[1]
    assert results[0].status_code == 500
    assert results[0].error == "http_500"
    assert results[1].status_code == 200


async def test_network_error_is_recorded_not_raised() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with _make_client(handler) as client:
        results = await run_virtual_user(
            client=client,
            base_url="http://agent",
            scenario=UC1_SNAPSHOT,
            user=UserContext(patient_id="p-4", token="tok-4"),
            clock=_fake_clock(),
            sleep=SleepSpy(),
        )

    assert len(results) == 1
    assert results[0].status_code is None
    assert results[0].error == "ConnectError"


async def test_abort_event_set_before_start_sends_no_requests() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"conversation_id": "c", "reply": "ok"})

    event = asyncio.Event()
    event.set()

    async with _make_client(handler) as client:
        results = await run_virtual_user(
            client=client,
            base_url="http://agent",
            scenario=UC2_FOLLOWUP,
            user=UserContext(patient_id="p-5", token="tok-5"),
            clock=_fake_clock(),
            sleep=SleepSpy(),
            abort_event=event,
        )

    assert results == []
    assert captured == []


# ---------------------------------------------------------------------------
# compute_ramp_delays — pure logic
# ---------------------------------------------------------------------------


def test_compute_ramp_delays_single_user_starts_immediately() -> None:
    assert compute_ramp_delays(1, 40.0) == [0.0]


def test_compute_ramp_delays_spreads_users_across_the_full_ramp_window() -> None:
    delays = compute_ramp_delays(5, 40.0)
    assert delays == [0.0, 10.0, 20.0, 30.0, 40.0]


def test_compute_ramp_delays_zero_ramp_starts_everyone_at_once() -> None:
    assert compute_ramp_delays(4, 0.0) == [0.0, 0.0, 0.0, 0.0]


def test_compute_ramp_delays_zero_users() -> None:
    assert compute_ramp_delays(0, 30.0) == []


# ---------------------------------------------------------------------------
# run_level — the abort guard must actually cut a level short
# ---------------------------------------------------------------------------


async def test_run_level_healthy_run_completes_every_step_for_every_user() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"conversation_id": "conv", "reply": "ok"})

    users = [UserContext(patient_id="p", token=f"tok-{i}") for i in range(3)]
    guard = AbortGuard(error_rate_threshold=0.5, dominance_threshold=0.5, sustained_seconds=60.0)

    async with _make_client(handler) as client:
        report = await run_level(
            client=client,
            base_url="http://agent",
            scenario=UC2_FOLLOWUP.with_think_time(0.0),
            users=users,
            ramp_seconds=0.0,
            guard=guard,
            clock=_fake_clock(),
            sleep=SleepSpy(),
        )

    assert report.aborted is False
    assert report.abort_reason is None
    # 3 users * 2 steps each = 6 total requests when nothing aborts.
    assert len(report.results) == 6


async def test_run_level_aborts_early_on_a_sustained_429_wall() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"detail": "rate limited"})

    users = [UserContext(patient_id="p", token=f"tok-{i}") for i in range(4)]
    # sustained_seconds=0.0 -> trips on the very first breaching observation,
    # matching the ticket's "stop immediately" guard.
    guard = AbortGuard(error_rate_threshold=0.5, dominance_threshold=0.5, sustained_seconds=0.0)

    async with _make_client(handler) as client:
        report = await run_level(
            client=client,
            base_url="http://agent",
            scenario=UC2_FOLLOWUP.with_think_time(0.0),
            users=users,
            ramp_seconds=0.0,
            guard=guard,
            clock=_fake_clock(),
            sleep=SleepSpy(),
        )

    assert report.aborted is True
    assert report.abort_reason is not None
    assert "429" in report.abort_reason
    # Would be 8 (4 users * 2 steps) if the guard did not cut the level
    # short — the whole point of the mandatory abort guard.
    assert len(report.results) < 8
    assert len(report.results) == 4  # one request per user; the followup never fires
