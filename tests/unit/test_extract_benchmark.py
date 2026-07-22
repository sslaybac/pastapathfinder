"""Walker coverage on the pinned Django benchmark (specs/tasks.md task 2.2).

design.md §3.5, D1a, §8-O5; `prototypes/engine-eval/FINDINGS-mypy.md` §2 trap 1 and Q2.

The walker is a hand-rolled copy of `mypy.traverser.TraverserVisitor`'s child map
(the compiled `@trait` cannot be subclassed), so "does it reach the code" is an empirical
question about real source, not a property of the map's shape. This module answers it the
way the prototype did: enumerate every call site with stdlib `ast`, walk the same files'
mypy trees, and compare position by position.

The benchmark tree is not vendored — the formal regression suite that fetches it by hash
arrives with task 4.4 — so this test is opt-in:

    PASTAPATHFINDER_DJANGO_BENCHMARK=/path/to/django-checkout .venv/bin/python -m pytest \\
        tests/unit/test_extract_benchmark.py -q -s

The path may be the repository root (the `django/` package inside it is what gets
analyzed, per the §8-O5 pin) or that package directory itself.
"""

import ast
import io
import os
import subprocess
import time
from collections import Counter
from pathlib import Path

import pytest

from pastapathfinder.adapters.base import SourceFile
from pastapathfinder.adapters.python import extract
from pastapathfinder.adapters.python.mypy_driver import run_build
from pastapathfinder.progress import ProgressSink

#: specs/tasks.md's benchmark pin, copied verbatim (design.md §8-O5).
DJANGO_COMMIT = "274df4df0bca7fcfb5c1c1d49567f770df147eeb"

#: `FINDINGS-harness.md` §2: the `django/` package is 908 `.py` files.
DJANGO_FILE_COUNT = 908

#: The task's bar. The prototype measured 37,207 / 37,218 = 99.97 % (0.03 % miss).
MINIMUM_COVERAGE = 0.999

ENV_VAR = "PASTAPATHFINDER_DJANGO_BENCHMARK"


def benchmark_package() -> Path:
    location = os.environ.get(ENV_VAR)
    if not location:
        pytest.skip(f"set {ENV_VAR} to the pinned Django checkout to run this measurement")
    root = Path(location).expanduser().resolve()
    package = root / "django" if (root / "django" / "__init__.py").is_file() else root
    if not (package / "__init__.py").is_file():
        pytest.skip(f"{package} is not the Django package directory")
    revision = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if revision and revision != DJANGO_COMMIT:
        pytest.skip(f"checkout is at {revision}, not the pinned {DJANGO_COMMIT}")
    return package


def ast_call_positions(path: Path) -> Counter:
    """`{(line, 0-based column): count}` for every `ast.Call` in a file."""
    tree = ast.parse(path.read_bytes(), filename=str(path))
    return Counter(
        (node.lineno, node.col_offset) for node in ast.walk(tree) if isinstance(node, ast.Call)
    )


def test_walker_coverage_on_the_pinned_django_benchmark(tmp_path):
    """≥ 99.9 % of enumerated call sites are reached by the structural walk."""
    package = benchmark_package()
    paths = sorted(package.rglob("*.py"))
    assert len(paths) == DJANGO_FILE_COUNT, f"{len(paths)} files — is this the pinned tree?"

    sources = [
        SourceFile(path=path, relpath=path.relative_to(package).as_posix()) for path in paths
    ]
    started = time.perf_counter()
    outcome = run_build(sources, tmp_path / "mypy_cache", ProgressSink(stream=io.StringIO()))
    build_seconds = time.perf_counter() - started

    started = time.perf_counter()
    enumerated = matched = 0
    for source in outcome.sources:
        tree = outcome.tree(source.relpath)
        if tree is None:  # pragma: no cover - a cold build gives every file a tree
            continue
        expected = ast_call_positions(source.path)
        found = Counter(
            (call.line, call.column)
            for call in extract.find_call_sites(tree)
            if call.line > 0 and call.column >= 0
        )
        enumerated += sum(expected.values())
        matched += sum(min(count, found[position]) for position, count in expected.items())
    walk_seconds = time.perf_counter() - started

    coverage = matched / enumerated
    print(
        f"\nwalker coverage: {matched}/{enumerated} = {coverage:.4%} "
        f"over {len(outcome.sources)} analyzed files "
        f"(engine build {build_seconds:.1f} s, walk {walk_seconds:.1f} s, "
        f"{len(outcome.skipped)} skipped)"
    )
    assert coverage >= MINIMUM_COVERAGE


def test_extraction_over_the_pinned_django_benchmark(tmp_path):
    """Every analyzed file extracts, and every row it produces is a §4.1/§4.2 row."""
    from pastapathfinder.schema import FileRecord, GraphFragment, validate_fragment

    package = benchmark_package()
    paths = sorted(package.rglob("*.py"))
    sources = [
        SourceFile(path=path, relpath=path.relative_to(package).as_posix()) for path in paths
    ]
    outcome = run_build(sources, tmp_path / "mypy_cache", ProgressSink(stream=io.StringIO()))
    index = extract.module_index(outcome.sources)

    started = time.perf_counter()
    extractions = {
        source.relpath: extract.extract_file(source, outcome.tree(source.relpath), index)
        for source in outcome.sources
        if outcome.tree(source.relpath) is not None
    }
    extract_seconds = time.perf_counter() - started

    known = {node.id for extraction in extractions.values() for node in extraction.nodes}
    nodes = sum(len(extraction.nodes) for extraction in extractions.values())
    edges = sum(len(extraction.edges) for extraction in extractions.values())
    diagnostics = sum(len(extraction.diagnostics) for extraction in extractions.values())
    for relpath, extraction in extractions.items():
        validate_fragment(
            GraphFragment(
                file=FileRecord(path=relpath, content_hash="0" * 64, status="analyzed"),
                nodes=list(extraction.nodes),
                edges=list(extraction.edges),
            ),
            known_ids=known,
        )
    print(
        f"\nextraction: {nodes} nodes, {edges} edges, {diagnostics} span diagnostics "
        f"over {len(extractions)} files in {extract_seconds:.1f} s"
    )
    assert len(extractions) == len(outcome.sources)
