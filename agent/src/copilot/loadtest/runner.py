"""Virtual-user / level runner (T030 criteria 1, 3, 5).

Drives the agent's ``POST /chat`` for one virtual user through a
:class:`~copilot.loadtest.scenarios.Scenario`, then orchestrates many
virtual users ramped up to a target concurrency for one measurement level
(criterion 1's "realistic snapshot + follow-up scenarios", criterion 3's
concurrency levels).

Two properties this module exists to get right, both asserted at the
transport seam in the tests (never via a return value, which would pass
against code that "did the right thing internally" but sent the wrong
request):

  * a follow-up step reuses the **same bearer token** and the
    **conversation_id from the prior response** — the T040 fix this ticket
    depends on for multi-turn conversations to avoid a 404.
  * the mandatory abort guard (``copilot.loadtest.abort_guard.AbortGuard``)
    is wired in per-result, not per-level — a level stops immediately once
    tripped, not after finishing every virtual user's queued turns.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

import httpx

from copilot.loadtest.abort_guard import AbortGuard
from copilot.loadtest.results import RequestResult
from copilot.loadtest.scenarios import Scenario

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]
Reporter = Callable[[RequestResult], None]


@dataclass(frozen=True)
class UserContext:
    """One virtual user's identity for the whole scenario: a real,
    T027-minted FHIR bearer bound to ``patient_id`` — never re-minted
    mid-scenario (that is what keeps a follow-up's token hash stable)."""

    patient_id: str
    token: str


def compute_ramp_delays(num_users: int, ramp_seconds: float) -> list[float]:
    """Stagger start delays across ``[0, ramp_seconds]`` — never slam
    straight to the target concurrency (T030 design decision)."""
    if num_users <= 0:
        return []
    if num_users == 1 or ramp_seconds <= 0:
        return [0.0] * num_users
    stagger = ramp_seconds / (num_users - 1)
    return [i * stagger for i in range(num_users)]


async def run_virtual_user(
    *,
    client: httpx.AsyncClient,
    base_url: str,
    scenario: Scenario,
    user: UserContext,
    clock: Clock = time.monotonic,
    sleep: Sleep = asyncio.sleep,
    report: Reporter | None = None,
    abort_event: "asyncio.Event | None" = None,
    guard_first_step: bool = True,
) -> list[RequestResult]:
    """Run one virtual user through every step of ``scenario`` in order.

    Checks ``abort_event`` before every step after the first, and again
    after each inter-step think-time sleep, so an in-flight abort stops a
    user before its *next* request rather than mid-request. Every step
    after the first is always gated this way, regardless of
    ``guard_first_step``.

    ``guard_first_step`` (default ``True``) controls only the very first
    step: with the default, a caller that hands in an already-set
    ``abort_event`` is telling this virtual user not to start at all —
    zero requests. ``run_level`` passes ``False`` for its own orchestrated
    users: once a user has been dispatched as part of a level (ramped in,
    already committed), its first request is not retroactively cancelled
    just because another concurrently-dispatched user's response happened
    to trip the guard first — only *new* requests (this user's own
    follow-up, or a not-yet-ramped-in user's first request, gated
    separately in ``run_level``) are prevented once tripped. This is the
    "in-flight work completes, no new work starts" circuit-breaker
    semantic, and keeps the abort guard's bound on wasted requests exactly
    as tight (at most one extra in-flight request per already-dispatched
    virtual user, never per not-yet-dispatched one) without cancelling
    work that was already fired.
    """
    results: list[RequestResult] = []
    conversation_id: str | None = None

    for i, step in enumerate(scenario.steps):
        if abort_event is not None and abort_event.is_set() and (i > 0 or guard_first_step):
            break
        if i > 0:
            await sleep(scenario.think_time_s)
            if abort_event is not None and abort_event.is_set():
                break

        payload: dict[str, object] = {
            "message": step.message,
            "patient_id": user.patient_id,
            "token": user.token,
        }
        if step.is_followup and conversation_id is not None:
            payload["conversation_id"] = conversation_id

        start = clock()
        try:
            response = await client.post(f"{base_url}/chat", json=payload)
            status_code: int | None = response.status_code
            if status_code == 200:
                body = response.json()
                conversation_id = body.get("conversation_id", conversation_id)
                error: str | None = None
            else:
                error = f"http_{status_code}"
        except httpx.HTTPError as exc:
            status_code = None
            error = type(exc).__name__

        latency_ms = (clock() - start) * 1000.0
        result = RequestResult(
            scenario=scenario.name,
            step=step.label,
            status_code=status_code,
            latency_ms=latency_ms,
            error=error,
            timestamp=clock(),
        )
        results.append(result)
        if report is not None:
            report(result)

    return results


@dataclass(frozen=True)
class LevelReport:
    results: list[RequestResult]
    aborted: bool
    abort_reason: str | None = None


async def run_level(
    *,
    client: httpx.AsyncClient,
    base_url: str,
    scenario: Scenario,
    users: list[UserContext],
    ramp_seconds: float,
    guard: AbortGuard,
    clock: Clock = time.monotonic,
    sleep: Sleep = asyncio.sleep,
) -> LevelReport:
    """Run one concurrency level: ramp ``users`` in over ``ramp_seconds``,
    feed every result to ``guard`` as it arrives, and stop the level from
    dispatching any *new* request the instant it trips.

    "New request" excludes each already-dispatched user's own first,
    already-committed request (see ``run_virtual_user``'s
    ``guard_first_step`` docstring) — a user ramped in as part of the same
    wave is not retroactively cancelled by a sibling's response landing a
    moment sooner. A user still waiting for its own ramp slot (``delay >
    0``) is never launched at all once the guard has tripped.
    """
    abort_event = asyncio.Event()
    all_results: list[RequestResult] = []
    trip_reason: list[str] = []

    def report(result: RequestResult) -> None:
        all_results.append(result)
        decision = guard.observe(result)
        if decision.tripped and not abort_event.is_set():
            trip_reason.append(decision.reason or "abort guard tripped")
            abort_event.set()

    delays = compute_ramp_delays(len(users), ramp_seconds)

    async def delayed_user(delay: float, user: UserContext) -> list[RequestResult]:
        if delay > 0:
            await sleep(delay)
            if abort_event.is_set():
                return []
        return await run_virtual_user(
            client=client,
            base_url=base_url,
            scenario=scenario,
            user=user,
            clock=clock,
            sleep=sleep,
            report=report,
            abort_event=abort_event,
            guard_first_step=False,
        )

    await asyncio.gather(
        *(delayed_user(delay, user) for delay, user in zip(delays, users))
    )

    return LevelReport(
        results=all_results,
        aborted=bool(trip_reason),
        abort_reason=trip_reason[0] if trip_reason else None,
    )
