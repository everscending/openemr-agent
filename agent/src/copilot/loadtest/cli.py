"""CLI entrypoint for running one load-test level against a live agent
(T030 criteria 1 and 3 — the harness's own executable entry point).

Thin argparse + file-IO wiring around the tested ``runner``/``results``
modules — the red/green-tested logic lives in those modules and in
``tests/test_loadtest_*.py``, not here. This module is what the measurement
runs recorded in ``docs/perf/`` actually invoked.

Example (local smoke, near-zero think-time, low concurrency)::

    uv run loadtest \\
        --base-url http://localhost:8380 \\
        --patient-id a23a078e-0da5-4b07-ab9e-ad99fbde1b89 \\
        --token-env COPILOT_LOADTEST_TOKEN \\
        --users 3 --ramp-seconds 3 --think-time-s 1 \\
        --out docs/perf/raw/local-smoke.ndjson

Example (deployed, ramped, mandated 10/50-user levels)::

    uv run loadtest \\
        --base-url https://copilot-agent-production-5c43.up.railway.app \\
        --patient-id <uuid> --token-env COPILOT_LOADTEST_TOKEN \\
        --users 50 --ramp-seconds 90 \\
        --out docs/perf/raw/deployed-50users.ndjson

The bearer token is read from an environment variable only — never a CLI
argument (would leak into shell history / process listings) and never
hardcoded in a committed file.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import httpx

from copilot.loadtest.abort_guard import AbortGuard
from copilot.loadtest.results import parse_results, stats_to_dict
from copilot.loadtest.runner import LevelReport, UserContext, run_level
from copilot.loadtest.scenarios import UC1_SNAPSHOT, UC2_FOLLOWUP, Scenario


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="T030 load-test level runner")
    parser.add_argument("--base-url", required=True, help="agent base URL, no trailing slash")
    parser.add_argument("--patient-id", required=True, help="FHIR patient uuid the token is bound to")
    parser.add_argument(
        "--token-env",
        default="COPILOT_LOADTEST_TOKEN",
        help="env var holding a real T027-minted FHIR bearer (never passed as a CLI arg)",
    )
    parser.add_argument("--users", type=int, required=True, help="target concurrency for this level")
    parser.add_argument("--ramp-seconds", type=float, default=30.0)
    parser.add_argument(
        "--scenario",
        choices=("uc1", "uc2"),
        default="uc2",
        help="uc2 (default) exercises both the snapshot and follow-up path per user",
    )
    parser.add_argument(
        "--think-time-s",
        type=float,
        default=None,
        help="override the scenario's documented think-time (e.g. 0-1s for a smoke run)",
    )
    parser.add_argument("--out", required=True, help="NDJSON raw-results output path")
    parser.add_argument("--error-rate-threshold", type=float, default=0.5)
    parser.add_argument("--dominance-threshold", type=float, default=0.5)
    parser.add_argument("--sustained-seconds", type=float, default=60.0)
    parser.add_argument("--window-seconds", type=float, default=60.0)
    parser.add_argument("--timeout-s", type=float, default=60.0)
    return parser


def _scenario_for(args: argparse.Namespace) -> Scenario:
    base = UC1_SNAPSHOT if args.scenario == "uc1" else UC2_FOLLOWUP
    if args.think_time_s is not None:
        return base.with_think_time(args.think_time_s)
    return base


async def _run(args: argparse.Namespace) -> LevelReport:
    token = os.environ.get(args.token_env)
    if not token:
        raise SystemExit(f"error: token env var {args.token_env} is not set")

    scenario = _scenario_for(args)
    users = [UserContext(patient_id=args.patient_id, token=token) for _ in range(args.users)]
    guard = AbortGuard(
        error_rate_threshold=args.error_rate_threshold,
        dominance_threshold=args.dominance_threshold,
        sustained_seconds=args.sustained_seconds,
        window_seconds=args.window_seconds,
    )

    async with httpx.AsyncClient(timeout=args.timeout_s) as client:
        return await run_level(
            client=client,
            base_url=args.base_url,
            scenario=scenario,
            users=users,
            ramp_seconds=args.ramp_seconds,
            guard=guard,
            clock=time.time,
        )


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    report = asyncio.run(_run(args))

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        for result in report.results:
            f.write(json.dumps(result.to_record()) + "\n")

    print(f"wrote {len(report.results)} raw results to {out_path}", file=sys.stderr)
    if report.aborted:
        print(f"ABORT GUARD TRIPPED: {report.abort_reason}", file=sys.stderr)

    if report.results:
        stats = parse_results([r.to_record() for r in report.results])
        print(json.dumps(stats_to_dict(stats), indent=2))
    else:
        print("no results collected", file=sys.stderr)
        return 1

    return 1 if report.aborted else 0


if __name__ == "__main__":
    raise SystemExit(main())
