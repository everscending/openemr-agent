"""Mandatory hard-abort guard for load-test levels (T030 design decision).

If a level's error rate exceeds ~50% sustained for 60s, or 429s dominate,
the level must stop immediately — rationale: the deployed agent is the
graded live demo, hours from the deadline, and a recorded 429 wall is
exactly the finding the PRD and ARCHITECTURE.md §10 expect, whereas
hammering through and rate-limiting the live demo is an unrecoverable loss.

Two independently-tracked failure signatures, both windowed and both
required to *sustain* before tripping (a brief blip must not abort a run):

  * hard-error rate — non-2xx/network-failure requests that are **not**
    429, as a fraction of the window. Whatever the incident is (5xx,
    timeouts), this is "the service is failing."
  * 429 dominance — 429 responses alone, as a fraction of the window. This
    is "the LLM provider is throttling us" — a distinct, expected finding
    (ARCHITECTURE.md §10's ~10K inflection arriving early), named as such
    rather than folded into an undifferentiated error rate.

See ``tests/test_loadtest_abort_guard.py`` — a guard never observed to trip
is not a guard, so it is exercised against both a healthy run (never trips)
and synthetic unhealthy runs for each failure signature (must trip).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from copilot.loadtest.results import RequestResult


@dataclass(frozen=True)
class AbortDecision:
    tripped: bool
    reason: str | None = None


@dataclass
class AbortGuard:
    error_rate_threshold: float = 0.5
    dominance_threshold: float = 0.5
    sustained_seconds: float = 60.0
    window_seconds: float = 60.0

    _window: list[RequestResult] = field(default_factory=list, repr=False)
    _breach_since: float | None = field(default=None, repr=False)

    def observe(self, result: RequestResult) -> AbortDecision:
        """Record one result and decide whether the level must abort now."""
        self._window.append(result)
        cutoff = result.timestamp - self.window_seconds
        self._window = [r for r in self._window if r.timestamp >= cutoff]

        total = len(self._window)
        rate_limited = sum(1 for r in self._window if r.status_code == 429)
        hard_errors = sum(
            1 for r in self._window if r.is_error() and r.status_code != 429
        )
        hard_error_rate = hard_errors / total if total else 0.0
        dominance_rate = rate_limited / total if total else 0.0

        hard_error_breach = hard_error_rate > self.error_rate_threshold
        dominance_breach = dominance_rate > self.dominance_threshold
        breach = hard_error_breach or dominance_breach

        if not breach:
            self._breach_since = None
            return AbortDecision(tripped=False)

        if self._breach_since is None:
            self._breach_since = result.timestamp

        sustained = result.timestamp - self._breach_since
        if sustained < self.sustained_seconds:
            return AbortDecision(tripped=False)

        if dominance_breach and not hard_error_breach:
            reason = (
                f"429 dominance={dominance_rate:.0%} of last {self.window_seconds:.0f}s "
                f"window, sustained {sustained:.0f}s — LLM provider rate limit wall"
            )
        elif hard_error_breach and not dominance_breach:
            reason = (
                f"hard error_rate={hard_error_rate:.0%} of last {self.window_seconds:.0f}s "
                f"window, sustained {sustained:.0f}s"
            )
        else:
            reason = (
                f"hard error_rate={hard_error_rate:.0%} AND 429 dominance="
                f"{dominance_rate:.0%} of last {self.window_seconds:.0f}s window, "
                f"sustained {sustained:.0f}s"
            )
        return AbortDecision(tripped=True, reason=reason)
