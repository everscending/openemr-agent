"""Tests for the mandatory hard-abort guard (T030 design decision).

"A guard you have never seen fail is not a guard" — this module drives the
guard with a synthetic unhealthy run and asserts it trips, and with a
healthy run and asserts it never does. Both directions are mandatory per
the brief.

Design decision under test: stop a level immediately if, sustained for
``sustained_seconds`` (nominally 60s), either (a) the windowed error rate
exceeds ``error_rate_threshold`` (nominally 50%), or (b) 429s alone exceed
``dominance_threshold`` of *all* windowed requests (nominally 50%) — a
distinct signal from (a) so a throttle wall is named as a throttle wall,
not folded into an undifferentiated "error rate" number.
"""

from __future__ import annotations

from copilot.loadtest.abort_guard import AbortGuard
from copilot.loadtest.results import RequestResult


def _result(
    *, t: float, status_code: int | None, error: str | None = None, scenario: str = "s"
) -> RequestResult:
    return RequestResult(
        scenario=scenario,
        step="step",
        status_code=status_code,
        latency_ms=100.0,
        error=error,
        timestamp=t,
    )


# ---------------------------------------------------------------------------
# Must NOT trip on a healthy run
# ---------------------------------------------------------------------------


def test_guard_never_trips_on_all_success_run() -> None:
    guard = AbortGuard(
        error_rate_threshold=0.5, dominance_threshold=0.5, sustained_seconds=60.0
    )

    decisions = [
        guard.observe(_result(t=float(i), status_code=200)) for i in range(0, 300, 1)
    ]

    assert all(not d.tripped for d in decisions)


def test_guard_does_not_trip_on_a_brief_error_burst_that_recovers() -> None:
    guard = AbortGuard(
        error_rate_threshold=0.5, dominance_threshold=0.5, sustained_seconds=60.0
    )

    decisions = []
    # 10s of pure errors (well under the 60s sustained requirement)...
    for t in range(0, 10):
        decisions.append(guard.observe(_result(t=float(t), status_code=500, error="http_500")))
    # ...then it recovers to all-success for a long stretch.
    for t in range(10, 200):
        decisions.append(guard.observe(_result(t=float(t), status_code=200)))

    assert all(not d.tripped for d in decisions)


# ---------------------------------------------------------------------------
# MUST trip — the guard has to be observed failing, not just passing
# ---------------------------------------------------------------------------


def test_guard_trips_on_sustained_hard_error_rate() -> None:
    guard = AbortGuard(
        error_rate_threshold=0.5, dominance_threshold=0.5, sustained_seconds=60.0
    )

    decisions = [
        guard.observe(_result(t=float(t), status_code=500, error="http_500"))
        for t in range(0, 90)
    ]

    tripped = [d for d in decisions if d.tripped]
    assert tripped, "guard never tripped on a 100% hard-error run sustained 90s"
    # reason must name the failure mode, not be a silent flag
    assert tripped[0].reason
    assert "error" in tripped[0].reason.lower() or "rate" in tripped[0].reason.lower()


def test_guard_trips_on_429_dominance_even_without_hard_errors() -> None:
    # A pure rate-limit wall: no 5xx, no network errors, only 429 — must
    # still be recognized and named as its own failure mode.
    guard = AbortGuard(
        error_rate_threshold=0.5, dominance_threshold=0.5, sustained_seconds=60.0
    )

    decisions = [
        guard.observe(_result(t=float(t), status_code=429, error="http_429"))
        for t in range(0, 90)
    ]

    tripped = [d for d in decisions if d.tripped]
    assert tripped, "guard never tripped on a 100% 429 run sustained 90s"
    assert "429" in tripped[0].reason


def test_guard_does_not_trip_before_sustained_window_elapses() -> None:
    guard = AbortGuard(
        error_rate_threshold=0.5, dominance_threshold=0.5, sustained_seconds=60.0
    )

    # Only 30s of continuous 100% errors — below the 60s sustained bar.
    decisions = [
        guard.observe(_result(t=float(t), status_code=500, error="http_500"))
        for t in range(0, 30)
    ]

    assert all(not d.tripped for d in decisions)


def test_guard_trips_at_error_rate_threshold_not_only_at_100_percent() -> None:
    # 60% error rate, sustained: crosses a 50% threshold without being a
    # total outage — this is the realistic "degraded, not dead" case.
    guard = AbortGuard(
        error_rate_threshold=0.5, dominance_threshold=0.9, sustained_seconds=60.0
    )

    decisions = []
    for t in range(0, 120):
        # 3 of every 5 requests error (60%), independent of 429 dominance
        # threshold (kept high here so only the error-rate path can trip).
        status = 500 if (t % 5) < 3 else 200
        error = "http_500" if status == 500 else None
        decisions.append(guard.observe(_result(t=float(t), status_code=status, error=error)))

    tripped = [d for d in decisions if d.tripped]
    assert tripped, "guard never tripped on a sustained 60% error rate"


def test_guard_window_expires_old_results_so_a_stale_burst_does_not_linger() -> None:
    guard = AbortGuard(
        error_rate_threshold=0.5,
        dominance_threshold=0.5,
        sustained_seconds=60.0,
        window_seconds=60.0,
    )

    # A short error burst...
    for t in range(0, 20):
        guard.observe(_result(t=float(t), status_code=500, error="http_500"))
    # ...ages out of the 60s window long before it could sustain a trip,
    # covered by a long run of clean traffic thereafter.
    decisions = [
        guard.observe(_result(t=float(t), status_code=200)) for t in range(20, 400)
    ]

    assert all(not d.tripped for d in decisions)
