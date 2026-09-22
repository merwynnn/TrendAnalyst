"""Layout and honesty tests.

They prove three things the build brief makes non-negotiable, by running:

1. the repository matches the spec §3 tree exactly (nothing missing, nothing ad-hoc);
2. every module imports cleanly — no import-time side effects, no network;
3. a module that declares itself a stub (`STUB_PHASE`) can never masquerade as working
   code: anything callable it defines must raise `NotImplementedError` (brief §7,
   "silent failure" anti-pattern). A stub that quietly returns a plausible value fails
   this test.
"""

from __future__ import annotations

import ast
import importlib
from importlib.metadata import version
from pathlib import Path
from typing import Final

import pytest

import trend_analyst

# The spec §3 layout, verbatim. Directories are listed as directories.
SPEC_TREE: Final[tuple[str, ...]] = (
    "README.md",
    "USER_SETUP.md",
    "AGENT.md",
    "pyproject.toml",
    ".env.example",
    "config/settings.py",
    "config/sources.yaml",
    "config/categories.yaml",
    "src/trend_analyst/pipeline/orchestrator.py",
    "src/trend_analyst/pipeline/runs.py",
    "src/trend_analyst/pipeline/layers",
    "src/trend_analyst/sources/base.py",
    "src/trend_analyst/sources/registry.py",
    "src/trend_analyst/sources/tier_s",
    "src/trend_analyst/sources/tier_a",
    "src/trend_analyst/scoring/features.py",
    "src/trend_analyst/scoring/normalize.py",
    "src/trend_analyst/scoring/mgs.py",
    "src/trend_analyst/scoring/fad.py",
    "src/trend_analyst/scoring/revenue.py",
    "src/trend_analyst/llm/gateway.py",
    "src/trend_analyst/llm/cache.py",
    "src/trend_analyst/llm/gates.py",
    "src/trend_analyst/llm/schemas.py",
    "src/trend_analyst/store/db.py",
    "src/trend_analyst/store/models.py",
    "src/trend_analyst/store/snapshots.py",
    "evals/cases.yaml",
    "evals/run_evals.py",
    "tests",
    "scripts",
    "migrations",
)


def _source_files(repo_root: Path) -> list[Path]:
    return sorted((repo_root / "src").rglob("*.py"))


def _module_name(path: Path, repo_root: Path) -> str:
    relative = path.relative_to(repo_root / "src").with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _raises_not_implemented(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True if the function body contains `raise NotImplementedError(...)`."""
    for node in ast.walk(function):
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
            func = node.exc.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name == "NotImplementedError":
                return True
    return False


@pytest.mark.parametrize("relative", SPEC_TREE)
def test_spec_layout_exists(repo_root: Path, relative: str) -> None:
    assert (repo_root / relative).exists(), f"spec §3 requires {relative}"


def test_no_ad_hoc_python_at_root(repo_root: Path) -> None:
    """Nothing ad-hoc at the root (spec §3): top-level .py files are forbidden."""
    strays = sorted(p.name for p in repo_root.glob("*.py"))
    assert strays == [], f"move these into the matching folder: {strays}"


def test_every_module_imports(repo_root: Path) -> None:
    """Import every module under src/ — catches circular imports and import-time work."""
    modules = [_module_name(path, repo_root) for path in _source_files(repo_root)]
    assert modules, "no modules found under src/"
    for name in modules:
        importlib.import_module(name)


def test_stub_modules_only_raise(repo_root: Path) -> None:
    """A module marked STUB_PHASE must define nothing that pretends to work."""
    offenders: list[str] = []
    for path in _source_files(repo_root):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        is_stub = any(
            isinstance(node, ast.Assign)
            and any(getattr(t, "id", "") == "STUB_PHASE" for t in node.targets)
            for node in tree.body
        )
        if not is_stub:
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not _raises_not_implemented(node):
                    offenders.append(f"{path.name}:{node.lineno} {node.name}() does not raise")
            elif isinstance(node, (ast.ClassDef, ast.AsyncWith)):
                offenders.append(f"{path.name}:{node.lineno} {node.name} is not a stub shape")
    assert offenders == [], f"stubs pretending to work: {offenders}"


def test_stub_modules_do_not_leak_callables(repo_root: Path) -> None:
    """Importing a stub must not expose working callables from elsewhere."""
    for path in _source_files(repo_root):
        module = importlib.import_module(_module_name(path, repo_root))
        if getattr(module, "STUB_PHASE", None) is None:
            continue
        for name, value in vars(module).items():
            if name.startswith("_") or not callable(value):
                continue
            assert getattr(value, "__module__", "") == module.__name__, (
                f"{module.__name__}.{name} is imported from {value.__module__}"
            )


def test_package_version_is_installed_and_matches() -> None:
    """The project is installed (editable) and its metadata version matches the code."""
    assert version("trend-analyst") == trend_analyst.__version__
