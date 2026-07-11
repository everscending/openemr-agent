#!/usr/bin/env bash
# T020 criterion 4: no secret is committed. ANTHROPIC_API_KEY must only ever
# appear in tracked files as an environment-variable reference
# (e.g. `${ANTHROPIC_API_KEY}`), never as a literal key value.
#
# Scans git-TRACKED files only (git ls-files — never .env, build artifacts,
# or anything gitignored), via the real git index, not a directory walk that
# would also catch untracked scratch files.
#
# Usage: agent/tests/infra/test_no_committed_secret.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"

python3 - "${REPO_ROOT}" <<'PYEOF'
from __future__ import annotations

import re
import subprocess
import sys

repo_root = sys.argv[1]

ASSIGNMENT = re.compile(r"ANTHROPIC_API_KEY\s*[:=]\s*")


def assigned_literal_value(line: str) -> str | None:
    """Return the assigned value's text if it is a literal (not a ``${...}``
    / ``$VAR`` interpolation reference and not empty), else ``None``.
    """
    match = ASSIGNMENT.search(line)
    if match is None:
        return None
    rest = line[match.end():]
    quote = rest[:1] if rest[:1] in ("\"", "'") else None
    if quote is not None:
        rest = rest[1:]
        end = rest.find(quote)
        value = rest[:end] if end != -1 else rest.rstrip("\n")
    else:
        parts = rest.split()
        value = parts[0] if parts else ""
    value = value.strip()
    if not value:
        return None
    if value.startswith("$"):  # ${ANTHROPIC_API_KEY} or $ANTHROPIC_API_KEY
        return None
    return value


tracked = subprocess.run(
    ["git", "-C", repo_root, "ls-files", "-z"],
    check=True,
    capture_output=True,
).stdout.split(b"\0")

violations = []
for raw_path in tracked:
    if not raw_path:
        continue
    path = raw_path.decode("utf-8", errors="surrogateescape")
    full_path = f"{repo_root}/{path}"
    try:
        with open(full_path, "r", encoding="utf-8", errors="ignore") as fh:
            for lineno, line in enumerate(fh, start=1):
                if "ANTHROPIC_API_KEY" not in line:
                    continue
                literal = assigned_literal_value(line)
                if literal is not None:
                    violations.append((path, lineno, line.strip()))
    except (IsADirectoryError, FileNotFoundError, PermissionError):
        continue

if violations:
    print("FAIL: possible committed ANTHROPIC_API_KEY secret(s):", file=sys.stderr)
    for path, lineno, line in violations:
        print(f"  {path}:{lineno}: {line}", file=sys.stderr)
    sys.exit(1)

print("PASS: no committed ANTHROPIC_API_KEY literal found in tracked files")
PYEOF
