"""Load and validate eval-case fixture files (T015).

ARCHITECTURE.md section 8 / the T015 design decisions: a malformed fixture
fails loudly with a *named* error identifying the offending file — never a
silent skip, never a warn-and-continue. Five named failure kinds are
distinguished: malformed YAML/JSON, a missing ``failure_mode``, an empty
``failure_mode``, an unregistered check name, and a duplicate case id.

An *empty* cases directory is deliberately not a loader error — it is a
zero-case load, which the runner/CLI treats as a failure in its own right
(a suite reporting success over zero cases is the vacuous guard again).
"""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from copilot.evals.checks import CHECK_REGISTRY
from copilot.evals.schema import EvalCase

_YAML_SUFFIXES = (".yaml", ".yml")
_JSON_SUFFIXES = (".json",)


class LoadErrorKind(str, Enum):
    """The named kind of a fixture load failure."""

    MALFORMED_YAML = "malformed_yaml"
    MISSING_FAILURE_MODE = "missing_failure_mode"
    EMPTY_FAILURE_MODE = "empty_failure_mode"
    UNKNOWN_CHECK = "unknown_check"
    DUPLICATE_ID = "duplicate_id"
    INVALID_SCHEMA = "invalid_schema"


class EvalLoadError(Exception):
    """A named, file-identifying fixture load failure.

    ``file`` is always the offending file's path — every load error names
    the file it came from. ``check_name`` is set only for
    :attr:`LoadErrorKind.UNKNOWN_CHECK`.
    """

    def __init__(
        self,
        kind: LoadErrorKind,
        file: Path,
        message: str,
        *,
        check_name: str | None = None,
    ) -> None:
        self.kind = kind
        self.file = file
        self.check_name = check_name
        super().__init__(f"{kind.value} in {file}: {message}")


def load_cases(directory: Path | str) -> tuple[EvalCase, ...]:
    """Load and validate every case file directly under ``directory``.

    Files are processed in sorted (filename) order for deterministic error
    reporting. A non-existent directory, or one with no recognized fixture
    files, yields an empty tuple — not an error (see the module docstring).
    """
    directory = Path(directory)
    if not directory.is_dir():
        return ()

    files = sorted(
        p
        for p in directory.iterdir()
        if p.is_file() and p.suffix in (*_YAML_SUFFIXES, *_JSON_SUFFIXES)
    )

    cases: list[EvalCase] = []
    seen_ids: dict[str, Path] = {}
    for file in files:
        raw = _parse_file(file)
        case = _build_case(raw, file)
        _validate_checks(case, file)
        if case.id in seen_ids:
            raise EvalLoadError(
                LoadErrorKind.DUPLICATE_ID,
                file,
                f"case id {case.id!r} is already defined in {seen_ids[case.id]}",
            )
        seen_ids[case.id] = file
        cases.append(case)
    return tuple(cases)


def _parse_file(file: Path) -> dict[str, Any]:
    text = file.read_text()
    if file.suffix in _JSON_SUFFIXES:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise EvalLoadError(
                LoadErrorKind.MALFORMED_YAML, file, f"invalid JSON: {exc}"
            ) from exc
    else:
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise EvalLoadError(
                LoadErrorKind.MALFORMED_YAML, file, f"invalid YAML: {exc}"
            ) from exc
    if not isinstance(data, dict):
        raise EvalLoadError(
            LoadErrorKind.MALFORMED_YAML,
            file,
            "top-level fixture content must be a mapping",
        )
    return data


def _build_case(raw: dict[str, Any], file: Path) -> EvalCase:
    try:
        return EvalCase.model_validate(raw)
    except ValidationError as exc:
        for err in exc.errors():
            if err["loc"] == ("failure_mode",):
                if err["type"] == "missing":
                    raise EvalLoadError(
                        LoadErrorKind.MISSING_FAILURE_MODE,
                        file,
                        "failure_mode is required",
                    ) from exc
                raise EvalLoadError(
                    LoadErrorKind.EMPTY_FAILURE_MODE,
                    file,
                    "failure_mode must be a non-empty string",
                ) from exc
        raise EvalLoadError(
            LoadErrorKind.INVALID_SCHEMA, file, str(exc)
        ) from exc


def _validate_checks(case: EvalCase, file: Path) -> None:
    for check in case.checks:
        if check.name not in CHECK_REGISTRY:
            raise EvalLoadError(
                LoadErrorKind.UNKNOWN_CHECK,
                file,
                f"unregistered check {check.name!r}",
                check_name=check.name,
            )
