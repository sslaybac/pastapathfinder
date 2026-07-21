"""The standing import-discipline guard (AC-23.1, AC-25.1).

design.md §3.4 makes the FR-23 adapter boundary "enforced by a unit test that greps
imports"; §3.11 puts the viewer behind the same test. Two rules, both permanent:

1. AC-23.1 — no module outside `src/pastapathfinder/adapters/python/` may import
   `mypy.*`. The engine is reached only through the adapter interface.
2. AC-25.1 — nothing under `src/pastapathfinder/viewer/` may import `mypy.*` or
   `pastapathfinder.adapters.*`. The viewer reads the index and nothing else.

Both pass vacuously while the tree is scaffolding; that is expected. This test must
never be weakened — `test_checker_*` below exist so that it cannot silently rot into
a no-op, and `test_scan_covers_the_whole_package` so that it cannot pass by scanning
an empty directory.
"""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "pastapathfinder"
ENGINE_PACKAGE = SRC_ROOT / "adapters" / "python"
VIEWER_PACKAGE = SRC_ROOT / "viewer"

ENGINE_MODULE = "mypy"
ADAPTERS_MODULE = "pastapathfinder.adapters"


def _covers(imported: str, forbidden: str) -> bool:
    """True when `imported` is `forbidden` itself or a submodule of it."""
    return imported == forbidden or imported.startswith(f"{forbidden}.")


def _dynamic_import_target(node: ast.Call) -> str | None:
    """The literal module name of an `__import__(...)` / `import_module(...)` call."""
    func = node.func
    is_dynamic_import = (isinstance(func, ast.Name) and func.id == "__import__") or (
        isinstance(func, ast.Attribute) and func.attr == "import_module"
    )
    if not is_dynamic_import or not node.args:
        return None
    first = node.args[0]
    return first.value if isinstance(first, ast.Constant) and isinstance(first.value, str) else None


def _imported_names(source: str, module_name: str, is_package: bool) -> set[str]:
    """Every absolute module name `source` imports, statically or dynamically.

    Relative imports are resolved against `module_name` so that, for example,
    `from ..adapters import base` inside `pastapathfinder.viewer.server` is seen as
    `pastapathfinder.adapters`.
    """
    package = module_name if is_package else module_name.rpartition(".")[0]
    names: set[str] = set()

    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base = node.module or ""
            else:
                parts = package.split(".")[: len(package.split(".")) - (node.level - 1)]
                base = ".".join([*parts, node.module] if node.module else parts)
            if base:
                names.add(base)
                names.update(f"{base}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Call):
            target = _dynamic_import_target(node)
            if target:
                names.add(target)

    return names


def _source_files() -> list[Path]:
    return sorted(SRC_ROOT.rglob("*.py"))


def _module_name(path: Path) -> str:
    relative = path.relative_to(SRC_ROOT.parent)
    parts = list(relative.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _violations() -> list[str]:
    """Every import that breaks rule 1 or rule 2, reported as `<file>: <import>`."""
    found: list[str] = []
    for path in _source_files():
        module_name = _module_name(path)
        imported = _imported_names(
            path.read_text(encoding="utf-8"), module_name, path.name == "__init__.py"
        )
        relative = path.relative_to(REPO_ROOT)
        in_engine_adapter = path.is_relative_to(ENGINE_PACKAGE)
        in_viewer = path.is_relative_to(VIEWER_PACKAGE)

        for name in sorted(imported):
            if _covers(name, ENGINE_MODULE) and not in_engine_adapter:
                found.append(f"{relative}: imports {name} outside the Python adapter (AC-23.1)")
            elif in_viewer and _covers(name, ADAPTERS_MODULE):
                found.append(f"{relative}: viewer imports {name} (AC-25.1)")
    return found


def test_scan_covers_the_whole_package():
    """Guard against the scan silently finding nothing if the tree is rearranged."""
    scanned = {str(p.relative_to(SRC_ROOT)) for p in _source_files()}
    assert len(scanned) >= 20
    assert {"cli.py", "viewer/server.py", "adapters/python/mypy_driver.py"} <= scanned


def test_engine_imports_are_confined_to_the_python_adapter():
    """AC-23.1: only `adapters/python/` may reference the engine's APIs."""
    offenders = [v for v in _violations() if "AC-23.1" in v]
    assert offenders == []


def test_viewer_imports_neither_engine_nor_adapters():
    """AC-25.1: the viewer's data comes from the index alone."""
    offenders = [v for v in _violations() if "AC-25.1" in v]
    assert offenders == []


def test_checker_detects_a_planted_engine_import():
    names = _imported_names("from mypy.build import build\n", "pastapathfinder.runner", False)
    assert _covers("mypy.build", ENGINE_MODULE)
    assert any(_covers(name, ENGINE_MODULE) for name in names)


def test_checker_resolves_relative_imports():
    names = _imported_names("from ..adapters import base\n", "pastapathfinder.viewer.server", False)
    assert ADAPTERS_MODULE in names


def test_checker_sees_dynamic_imports():
    names = _imported_names(
        "import importlib\nm = importlib.import_module('mypy.build')\n",
        "pastapathfinder.runner",
        False,
    )
    assert "mypy.build" in names
    assert "mypy" in _imported_names("__import__('mypy')\n", "pastapathfinder.runner", False)
