"""Deterministic eval-case harness (T015).

ARCHITECTURE.md section 8: eval cases live as versioned fixtures in the repo
(the platform is only a runner/viewer). This package holds the case schema
(:mod:`copilot.evals.schema`), the fixture loader
(:mod:`copilot.evals.loader`), the named check registry
(:mod:`copilot.evals.checks`), the mock-FHIR-transport builder
(:mod:`copilot.evals.fhir_mock`), and the runner
(:mod:`copilot.evals.runner`) that executes a case end-to-end against the
in-process app with no network. Invoke as ``python -m copilot.evals``.
"""

from __future__ import annotations
