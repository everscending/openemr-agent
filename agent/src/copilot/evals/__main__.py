"""CLI entry point: ``python -m copilot.evals [cases_dir]`` (T015).

CI-ready: a machine-readable JSON report goes to **stdout** (so
``... | jq`` works); a human summary goes to **stderr**. Exit code is ``0``
only when at least one case ran and every case passed — non-zero on any
failing case, any load error, and an empty cases directory (a suite that
"succeeds" over zero cases is the vacuous guard the design decisions call
out explicitly).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from copilot.evals.loader import EvalLoadError, load_cases
from copilot.evals.runner import run_cases

DEFAULT_CASES_DIR = "evals/cases"


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="copilot.evals",
        description="Run the deterministic eval-case fixture suite.",
    )
    parser.add_argument(
        "cases_dir",
        nargs="?",
        default=DEFAULT_CASES_DIR,
        help=f"Directory of eval-case fixture files (default: {DEFAULT_CASES_DIR})",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    cases_dir = Path(args.cases_dir)

    try:
        cases = load_cases(cases_dir)
    except EvalLoadError as exc:
        print(
            json.dumps(
                {
                    "error": str(exc),
                    "kind": exc.kind.value,
                    "file": str(exc.file),
                }
            )
        )
        print(f"eval load error: {exc}", file=sys.stderr)
        return 1

    if not cases:
        print(
            json.dumps(
                {
                    "error": "no cases found",
                    "cases_dir": str(cases_dir),
                    "cases": [],
                    "total": 0,
                    "passed": 0,
                    "failed": 0,
                }
            )
        )
        print(f"no cases found in {cases_dir}", file=sys.stderr)
        return 1

    reports = asyncio.run(run_cases(cases))
    passed = sum(1 for r in reports if r.passed)
    failed = len(reports) - passed
    payload = {
        "cases": [r.model_dump(mode="json") for r in reports],
        "total": len(reports),
        "passed": passed,
        "failed": failed,
    }
    print(json.dumps(payload))

    for r in reports:
        status = "PASS" if r.passed else "FAIL"
        print(f"[{status}] {r.id} ({r.guards_against.value})", file=sys.stderr)
        for reason in r.reasons:
            print(f"    - {reason}", file=sys.stderr)
    print(f"{passed}/{len(reports)} cases passed", file=sys.stderr)

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
