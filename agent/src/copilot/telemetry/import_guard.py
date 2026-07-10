"""AST-based portability guard: no vendor tracing SDK anywhere but one file
(T014 criterion 4).

ARCHITECTURE.md:422 — agent code never imports a vendor tracing SDK. T008
shipped an import scan that only inspected each module's own AST in
isolation and passed while the module transitively imported ``httpx`` two
hops away, caught only by the orchestrator. A guard never observed to catch
a violation is not a guard, so this module is exercised directly by
synthetic planted-violation tests (see ``tests/test_observability.py``)
*and* walks the real import graph rather than checking one file at a time.

Scope, deliberately: this scans **module-level** (top-of-file) ``import`` /
``from ... import`` statements only. An import nested inside a function or
method body is not followed — that mirrors this codebase's own established
containment idiom (e.g. ``copilot.app._default_chat_llm`` lazily imports the
Anthropic SDK inside a function body specifically so importing the module
does not pull it in; T010's own import-purity tests check ``sys.modules``
*after* a bare ``import`` for exactly this reason). The one bootstrap module
permitted to import the OTel SDK/exporter directly is excluded from its own
violation report via ``exempt_modules`` — but only for itself: nothing that
reaches it through a module-level import escapes the check, since the
bootstrap module's own direct imports still propagate through the graph to
any (non-exempt) importer.

Pure module: no OpenTelemetry import of any kind — this file must be clean
by its own rule.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

#: Vendor tracing/observability SDKs and the OTel SDK/exporter surface — none
#: of these may be imported by instrumented code (ARCHITECTURE.md §7's
#: portability rule). ``opentelemetry.trace`` (the API) is deliberately not
#: on this list — it is the vendor-neutral surface, permitted everywhere.
BANNED_MODULES: tuple[str, ...] = (
    "langsmith",
    "braintrust",
    "langfuse",
    "datadog",
    "newrelic",
    "sentry_sdk",
    "opentelemetry.sdk",
    "opentelemetry.exporter",
)

#: The one module allowed to import the OTel SDK/exporter directly, wired
#: lazily from ``create_app`` (never at another module's top level).
EXEMPT_MODULES: frozenset[str] = frozenset({"copilot.telemetry.bootstrap"})


@dataclass(frozen=True)
class ModuleImports:
    """One module's top-level import classification."""

    #: Banned vendor modules this module imports directly.
    banned: frozenset[str] = field(default_factory=frozenset)
    #: First-party dotted module names this module imports directly — the
    #: edges used to walk the transitive closure.
    local: frozenset[str] = field(default_factory=frozenset)


def scan_source(
    source: str,
    *,
    banned_modules: Sequence[str] = BANNED_MODULES,
    first_party_prefix: str = "copilot",
) -> ModuleImports:
    """Classify one module's top-level imports.

    Only statements that are direct children of the module body are
    considered — imports nested inside a function/class/branch are not
    (see the module docstring's "Scope" note). ``banned`` matches a dotted
    import that equals, or is a submodule of, any entry in
    ``banned_modules`` (so ``opentelemetry.sdk.trace`` matches the banned
    ``opentelemetry.sdk``).
    """
    tree = ast.parse(source)
    banned_hits: set[str] = set()
    local_hits: set[str] = set()

    def classify(dotted: str) -> None:
        for banned in banned_modules:
            if dotted == banned or dotted.startswith(banned + "."):
                banned_hits.add(banned)
        if dotted == first_party_prefix or dotted.startswith(first_party_prefix + "."):
            local_hits.add(dotted)

    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                classify(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                continue  # relative import; unused by this codebase, skip safely
            module = node.module or ""
            classify(module)
            if module == first_party_prefix or module.startswith(
                first_party_prefix + "."
            ):
                # `from copilot.agent import loop` may import the *submodule*
                # `copilot.agent.loop`, not just a symbol inside `agent`'s
                # __init__ — register the combined path too, so the
                # transitive walk below can follow it if it resolves to a
                # real file. A nonexistent combination simply contributes no
                # edges (see scan_tree).
                for alias in node.names:
                    local_hits.add(f"{module}.{alias.name}")

    return ModuleImports(banned=frozenset(banned_hits), local=frozenset(local_hits))


def _module_name(root: Path, file: Path) -> str:
    rel = file.relative_to(root.parent)
    parts = list(rel.parts)
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1][: -len(".py")]
    return ".".join(parts)


def _closure(
    module: str,
    imports_by_module: dict[str, ModuleImports],
    seen: set[str],
) -> set[str]:
    if module in seen:
        return set()
    seen.add(module)
    info = imports_by_module.get(module)
    if info is None:
        return set()
    reached = set(info.banned)
    for local in info.local:
        reached |= _closure(local, imports_by_module, seen)
    return reached


def scan_tree(
    root: Path,
    *,
    exempt_modules: frozenset[str] = EXEMPT_MODULES,
    banned_modules: Sequence[str] = BANNED_MODULES,
    first_party_prefix: str | None = None,
) -> dict[str, frozenset[str]]:
    """Walk every ``.py`` file under ``root`` and report import-graph violations.

    Returns ``{dotted_module_name: banned_modules_reachable}`` for every
    module (excluding ``exempt_modules``) whose own imports, or whose
    *transitively* imported first-party modules' imports, reach a banned
    vendor module — closing exactly the gap T008 missed (a violation two
    hops away via a normal first-party import). An empty dict means no
    violations.

    ``first_party_prefix`` defaults to ``root.name`` (e.g. scanning the
    ``copilot`` package directory classifies ``copilot.*`` imports as
    first-party); tests scanning a synthetic package pass a matching prefix
    explicitly.
    """
    prefix = first_party_prefix if first_party_prefix is not None else root.name
    files = sorted(root.rglob("*.py"))

    imports_by_module: dict[str, ModuleImports] = {}
    for f in files:
        dotted = _module_name(root, f)
        imports_by_module[dotted] = scan_source(
            f.read_text(),
            banned_modules=banned_modules,
            first_party_prefix=prefix,
        )

    violations: dict[str, frozenset[str]] = {}
    for module in imports_by_module:
        if module in exempt_modules:
            continue
        reached = _closure(module, imports_by_module, set())
        if reached:
            violations[module] = frozenset(reached)
    return violations
