"""Load-test results parsing (T030 criterion 2).

Turns raw per-request result records (as written by the harness's runner,
one JSON object per line — see :func:`load_results_ndjson`) into the
p50/p95/p99 latency and error-rate numbers the PRD requires recording
(PRD.md:334-340). A malformed or empty input raises :class:`ResultsParseError`
rather than silently reporting "0 errors, great numbers" — a truncated or
missing results file must never read as a passing run.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

_REQUIRED_KEYS = ("scenario", "step", "status_code", "latency_ms", "error", "timestamp")


class ResultsParseError(ValueError):
    """A results record, or a whole results file, could not be parsed."""


@dataclass(frozen=True)
class RequestResult:
    """One `/chat` call's outcome, as recorded by a virtual user."""

    scenario: str
    step: str
    status_code: int | None
    latency_ms: float
    error: str | None
    timestamp: float

    def is_error(self) -> bool:
        """True if this request did not cleanly succeed.

        A missing status code (transport-level failure), any non-2xx
        status, or an explicit ``error`` marker all count — a 429 counts as
        an error here (it still shows up in the general error rate) even
        though :mod:`copilot.loadtest.abort_guard` tracks it as its own,
        separately named failure mode.
        """
        if self.error is not None:
            return True
        if self.status_code is None:
            return True
        return self.status_code >= 400

    def to_record(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "step": self.step,
            "status_code": self.status_code,
            "latency_ms": self.latency_ms,
            "error": self.error,
            "timestamp": self.timestamp,
        }

    @staticmethod
    def from_record(record: dict[str, Any]) -> "RequestResult":
        missing = [k for k in _REQUIRED_KEYS if k not in record]
        if missing:
            raise ResultsParseError(f"result record missing required key(s): {missing}")

        scenario = record["scenario"]
        step = record["step"]
        status_code = record["status_code"]
        latency_ms = record["latency_ms"]
        error = record["error"]
        timestamp = record["timestamp"]

        if not isinstance(scenario, str) or not scenario:
            raise ResultsParseError("result record 'scenario' must be a non-empty string")
        if not isinstance(step, str) or not step:
            raise ResultsParseError("result record 'step' must be a non-empty string")
        if status_code is not None and not isinstance(status_code, int):
            raise ResultsParseError("result record 'status_code' must be an int or null")
        if isinstance(latency_ms, bool) or not isinstance(latency_ms, (int, float)):
            raise ResultsParseError("result record 'latency_ms' must be numeric")
        if latency_ms < 0:
            raise ResultsParseError("result record 'latency_ms' must not be negative")
        if error is not None and not isinstance(error, str):
            raise ResultsParseError("result record 'error' must be a string or null")
        if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
            raise ResultsParseError("result record 'timestamp' must be numeric")

        return RequestResult(
            scenario=scenario,
            step=step,
            status_code=status_code,
            latency_ms=float(latency_ms),
            error=error,
            timestamp=float(timestamp),
        )


@dataclass(frozen=True)
class LoadTestStats:
    total_requests: int
    error_count: int
    error_rate: float
    rate_limited_count: int
    p50_ms: float
    p95_ms: float
    p99_ms: float


def _percentile(sorted_values: Sequence[float], pct: float) -> float:
    if not sorted_values:
        raise ResultsParseError("cannot compute a percentile of zero latency samples")
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (len(sorted_values) - 1) * pct
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_values[int(k)]
    d0 = sorted_values[f] * (c - k)
    d1 = sorted_values[c] * (k - f)
    return d0 + d1


def parse_results(records: Sequence[dict[str, Any]]) -> LoadTestStats:
    """Compute :class:`LoadTestStats` from raw result records.

    Raises :class:`ResultsParseError` on empty input or any record that
    fails :meth:`RequestResult.from_record` validation — never returns a
    "clean" stats object for input that was not actually a real run.
    """
    if not records:
        raise ResultsParseError("no results to parse (empty input)")

    parsed = [RequestResult.from_record(r) for r in records]
    latencies = sorted(r.latency_ms for r in parsed)
    errors = [r for r in parsed if r.is_error()]
    rate_limited = [r for r in parsed if r.status_code == 429]
    total = len(parsed)

    return LoadTestStats(
        total_requests=total,
        error_count=len(errors),
        error_rate=len(errors) / total,
        rate_limited_count=len(rate_limited),
        p50_ms=_percentile(latencies, 0.50),
        p95_ms=_percentile(latencies, 0.95),
        p99_ms=_percentile(latencies, 0.99),
    )


def load_results_ndjson(path: Path) -> list[dict[str, Any]]:
    """Read a newline-delimited-JSON results file into raw record dicts.

    Raises :class:`ResultsParseError` on an empty file, a line that is not
    valid JSON, or a line whose JSON value is not an object — a truncated
    write must surface loudly, not parse as "no records".
    """
    text = path.read_text()
    if not text.strip():
        raise ResultsParseError(f"results file is empty: {path}")

    records: list[dict[str, Any]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ResultsParseError(
                f"malformed JSON on line {lineno} of {path}"
            ) from exc
        if not isinstance(obj, dict):
            raise ResultsParseError(f"line {lineno} of {path} is not a JSON object")
        records.append(obj)

    if not records:
        raise ResultsParseError(f"results file has no records: {path}")
    return records


def stats_to_dict(stats: LoadTestStats) -> dict[str, Any]:
    return {
        "total_requests": stats.total_requests,
        "error_count": stats.error_count,
        "error_rate": stats.error_rate,
        "rate_limited_count": stats.rate_limited_count,
        "p50_ms": stats.p50_ms,
        "p95_ms": stats.p95_ms,
        "p99_ms": stats.p99_ms,
    }
