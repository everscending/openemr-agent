"""CLI entrypoint for running one load-test level through the OpenEMR
public relay (T030 re-scope, 2026-07-12) — the real user path (browser ->
OpenEMR relay -> agent), used because the agent has no public route and the
re-scope decision was to not give it one.

Thin argparse + file-IO wiring around the tested ``relay_transport`` module
(and the pre-existing, reused ``results``/``abort_guard`` modules) — the
red/green-tested logic lives there and in
``tests/test_loadtest_relay_transport.py``, not here.

Credentials come from environment variables only — never a CLI argument,
never hardcoded. If login fails, this makes no attempt to guess or retry
with a different password; it reports whatever the relay transport recorded
(a ``"login"`` step result per user) and exits non-zero.

Example::

    export OE_LOAD_USER=admin OE_LOAD_PASS=pass
    uv run loadtest-relay \\
        --base-url https://openemr-production-472c.up.railway.app \\
        --patient-pid 2 \\
        --users 10 --ramp-seconds 60 \\
        --out docs/perf/raw/deployed-relay-10users.ndjson
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from copilot.loadtest.abort_guard import AbortGuard
from copilot.loadtest.relay_transport import RelayLevelReport, RelayUser, run_relay_level
from copilot.loadtest.results import parse_results, stats_to_dict
from copilot.loadtest.scenarios import UC1_SNAPSHOT, UC2_FOLLOWUP, Scenario


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="T030 relay-transport load-test level runner")
    parser.add_argument("--base-url", required=True, help="OpenEMR base URL, no trailing slash")
    parser.add_argument("--patient-pid", type=int, required=True, help="the demo patient's pid, e.g. 2")
    parser.add_argument("--user-env", default="OE_LOAD_USER")
    parser.add_argument("--pass-env", default="OE_LOAD_PASS")
    parser.add_argument("--users", type=int, required=True)
    parser.add_argument("--ramp-seconds", type=float, default=30.0)
    parser.add_argument("--scenario", choices=("uc1", "uc2"), default="uc2")
    parser.add_argument("--think-time-s", type=float, default=None)
    parser.add_argument("--out", required=True)
    parser.add_argument("--error-rate-threshold", type=float, default=0.5)
    parser.add_argument("--dominance-threshold", type=float, default=0.5)
    parser.add_argument("--sustained-seconds", type=float, default=60.0)
    parser.add_argument("--window-seconds", type=float, default=60.0)
    parser.add_argument("--timeout-s", type=float, default=60.0)
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="skip TLS verification (local dev stack's self-signed cert only — never for a deployed run)",
    )
    return parser


def _scenario_for(args: argparse.Namespace) -> Scenario:
    base = UC1_SNAPSHOT if args.scenario == "uc1" else UC2_FOLLOWUP
    if args.think_time_s is not None:
        return base.with_think_time(args.think_time_s)
    return base


async def _run(args: argparse.Namespace) -> RelayLevelReport:
    username = os.environ.get(args.user_env, "admin")
    password = os.environ.get(args.pass_env, "pass")

    scenario = _scenario_for(args)
    users = [RelayUser(username=username, password=password) for _ in range(args.users)]
    guard = AbortGuard(
        error_rate_threshold=args.error_rate_threshold,
        dominance_threshold=args.dominance_threshold,
        sustained_seconds=args.sustained_seconds,
        window_seconds=args.window_seconds,
    )

    def client_factory():
        import httpx

        return httpx.AsyncClient(timeout=args.timeout_s, verify=not args.insecure)

    return await run_relay_level(
        base_url=args.base_url,
        scenario=scenario,
        users=users,
        patient_pid=args.patient_pid,
        ramp_seconds=args.ramp_seconds,
        guard=guard,
        clock=time.time,
        client_factory=client_factory,
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

    login_failures = [r for r in report.results if r.step == "login"]
    if login_failures:
        print(
            f"LOGIN FAILED for {len(login_failures)}/{len(report.results)} virtual user(s) "
            f"— check --user-env/--pass-env credentials. Not retrying/guessing.",
            file=sys.stderr,
        )

    if report.aborted:
        print(f"ABORT GUARD TRIPPED: {report.abort_reason}", file=sys.stderr)

    if report.results:
        stats = parse_results([r.to_record() for r in report.results])
        print(json.dumps(stats_to_dict(stats), indent=2))
    else:
        print("no results collected", file=sys.stderr)
        return 1

    if login_failures and len(login_failures) == len(report.results):
        return 2  # every user failed to authenticate — nothing was measured
    return 1 if report.aborted else 0


if __name__ == "__main__":
    raise SystemExit(main())
