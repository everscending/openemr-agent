"""Tests for the load-test results parser (T030 criterion 2).

The harness's raw output is a stream of per-request result records (status
code, latency, error); this module turns that into the p50/p95/p99 latency
and error-rate numbers the ticket requires recording. Criteria map:

  2. Committed results must report p50/p95/p99 latency and error rate —
     ``parse_results`` computes these from raw records.
  +  Mandatory adversarial requirement from the brief: "The results parser
     must be tested against a known-bad fixture too — a malformed / empty
     stats file must not silently yield '0 errors, great numbers.'" See
     ``test_parse_results_rejects_empty_input``,
     ``test_parse_results_rejects_record_with_wrong_typed_field``,
     ``test_parse_results_rejects_record_missing_required_key``,
     ``test_load_results_ndjson_rejects_malformed_json_line``,
     ``test_load_results_ndjson_rejects_empty_file``.
"""

from __future__ import annotations

import pytest

from copilot.loadtest.results import (
    RequestResult,
    ResultsParseError,
    load_results_ndjson,
    parse_results,
    stats_to_dict,
)


def _ok_record(
    *,
    latency_ms: float,
    status_code: int | None = 200,
    error: str | None = None,
    scenario: str = "UC-1-snapshot",
    step: str = "initial-snapshot",
    timestamp: float = 0.0,
) -> dict:
    return {
        "scenario": scenario,
        "step": step,
        "status_code": status_code,
        "latency_ms": latency_ms,
        "error": error,
        "timestamp": timestamp,
    }


# ---------------------------------------------------------------------------
# Criterion 2 — correct percentile / error-rate computation
# ---------------------------------------------------------------------------


def test_parse_results_computes_p50_p95_p99_by_linear_interpolation() -> None:
    # 100 latencies 1..100ms — a textbook linear-interpolation percentile
    # example (independently verified: p50=50.5, p95=95.05, p99=99.01).
    records = [_ok_record(latency_ms=float(i)) for i in range(1, 101)]

    stats = parse_results(records)

    assert stats.total_requests == 100
    assert stats.p50_ms == pytest.approx(50.5)
    assert stats.p95_ms == pytest.approx(95.05)
    assert stats.p99_ms == pytest.approx(99.01)


def test_parse_results_computes_error_rate_and_rate_limited_count() -> None:
    records = [_ok_record(latency_ms=100.0) for _ in range(7)]
    records += [_ok_record(latency_ms=200.0, status_code=500, error="http_500")]
    records += [_ok_record(latency_ms=50.0, status_code=500, error="http_500")]
    records += [_ok_record(latency_ms=10.0, status_code=429, error="http_429")]

    stats = parse_results(records)

    assert stats.total_requests == 10
    assert stats.error_count == 3
    assert stats.error_rate == pytest.approx(0.3)
    assert stats.rate_limited_count == 1


def test_parse_results_treats_network_failure_as_error() -> None:
    # No HTTP status at all (transport-level failure) still counts as an
    # error — a run that never got a response must not be scored as clean.
    records = [_ok_record(latency_ms=100.0)]
    records.append(_ok_record(latency_ms=30000.0, status_code=None, error="ConnectTimeout"))

    stats = parse_results(records)

    assert stats.error_count == 1
    assert stats.error_rate == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Mandatory adversarial: malformed / empty input must raise, never green-wash
# ---------------------------------------------------------------------------


def test_parse_results_rejects_empty_input() -> None:
    with pytest.raises(ResultsParseError):
        parse_results([])


def test_parse_results_rejects_record_missing_required_key() -> None:
    bad = _ok_record(latency_ms=10.0)
    del bad["status_code"]

    with pytest.raises(ResultsParseError):
        parse_results([bad])


def test_parse_results_rejects_record_with_wrong_typed_field() -> None:
    # latency_ms as a string must not be silently coerced (strict typing —
    # a "fast" string could otherwise sort as 0 and vanish the outage).
    bad = _ok_record(latency_ms=10.0)
    bad["latency_ms"] = "fast"

    with pytest.raises(ResultsParseError):
        parse_results([bad])


def test_parse_results_rejects_negative_latency() -> None:
    bad = _ok_record(latency_ms=-5.0)

    with pytest.raises(ResultsParseError):
        parse_results([bad])


def test_load_results_ndjson_rejects_malformed_json_line(tmp_path) -> None:
    path = tmp_path / "results.ndjson"
    path.write_text('{"scenario": "UC-1"\n')  # truncated JSON, missing brace

    with pytest.raises(ResultsParseError):
        load_results_ndjson(path)


def test_load_results_ndjson_rejects_empty_file(tmp_path) -> None:
    path = tmp_path / "results.ndjson"
    path.write_text("")

    with pytest.raises(ResultsParseError):
        load_results_ndjson(path)


def test_load_results_ndjson_parses_real_records_and_round_trips_through_parse(
    tmp_path,
) -> None:
    path = tmp_path / "results.ndjson"
    lines = [_ok_record(latency_ms=float(i)) for i in range(1, 11)]
    import json

    path.write_text("\n".join(json.dumps(r) for r in lines) + "\n")

    records = load_results_ndjson(path)
    stats = parse_results(records)

    assert stats.total_requests == 10
    assert stats.error_count == 0


def test_stats_to_dict_is_json_serializable_and_carries_all_fields() -> None:
    import json

    records = [_ok_record(latency_ms=float(i)) for i in range(1, 11)]
    stats = parse_results(records)

    payload = stats_to_dict(stats)
    serialized = json.dumps(payload)  # must not raise

    reloaded = json.loads(serialized)
    assert reloaded["total_requests"] == 10
    assert reloaded["error_count"] == 0
    assert "p50_ms" in reloaded and "p95_ms" in reloaded and "p99_ms" in reloaded
    assert "error_rate" in reloaded and "rate_limited_count" in reloaded


def test_request_result_from_record_round_trips() -> None:
    record = _ok_record(latency_ms=42.0, status_code=200)
    result = RequestResult.from_record(record)

    assert result.latency_ms == 42.0
    assert result.status_code == 200
    assert result.is_error() is False
    assert result.to_record() == record
