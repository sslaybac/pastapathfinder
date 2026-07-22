"""The engine boundary: options, pre-flight, the rechecked report, and the fallback.

design.md §3.5 (`mypy_driver`, normative), §3.10 (`progress`), D1, D1a, D6 rule 1, D13;
requirements FR-6 (AC-6.1, AC-6.2), FR-13 (AC-13.1, AC-13.2), FR-24 (AC-24.3),
FR-30 (AC-30.2), FR-41 (AC-41.2), EC-12.

Most tests here drive the real engine over a real tree, because every property under
test — does a sibling import resolve, does a warm build recheck anything, does a corrupt
cache crash the build — is a property of mypy 2.3.0 and not of any double. A cold build
of a three-file package costs about a second.

Three tests use the `_invoke_build` seam instead, for states the engine will not enter on
demand: a build that fails twice, a build slow enough to time a heartbeat against, and a
result that omits a file it was handed. They are labelled where they appear.

The traps of `FINDINGS-mypy.md` §2 are reproduced rather than rediscovered — each has a
test that fails if the discipline is dropped, since all three are silent in production
(wrong `mypy_path` costs recall with no error at all).
"""

import io
import os
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from pastapathfinder import reports
from pastapathfinder.adapters.base import SourceFile
from pastapathfinder.adapters.python import mypy_driver
from pastapathfinder.adapters.python.mypy_driver import (
    ENGINE_NAME,
    ENGINE_VERSION,
    FALLBACK_NOTICE,
    PHASE_BUILD,
    PINNED_ENGINE_VERSION,
    BuildOutcome,
    EngineError,
    build_options,
    prepare_sources,
    run_build,
)
from pastapathfinder.progress import PROGRESS_INTERVAL_SECONDS, ProgressSink

AS_ROOT = os.geteuid() == 0

#: A package whose modules import each other by their absolute names — the shape every
#: real package has, and the one trap 1 is about.
SIBLING_PACKAGE = {
    "pkg/__init__.py": "",
    "pkg/other.py": "def helper() -> int:\n    return 1\n",
    "pkg/good.py": "from pkg.other import helper\n\n\ndef top() -> int:\n    return helper()\n",
}


def make_tree(root: Path, files: Mapping[str, str | bytes]) -> Path:
    """Write `relpath -> content` under `root`, creating parents."""
    for relpath, content in files.items():
        path = root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
    return root


def sources_under(root: Path, *relpaths: str) -> list[SourceFile]:
    """`SourceFile`s for `relpaths`, spelled the way discovery spells them."""
    return [SourceFile(path=root / relpath, relpath=relpath) for relpath in relpaths]


def quiet() -> ProgressSink:
    """A progress sink whose output is captured rather than printed."""
    return ProgressSink(stream=io.StringIO())


def run(root: Path, cache: Path, *relpaths: str, progress: ProgressSink | None = None):
    """Build over `relpaths` under `root`, with output captured by default."""
    return run_build(sources_under(root, *relpaths), cache, progress or quiet())


def skips(outcome: BuildOutcome) -> dict[str, str]:
    """`{relpath: reason class}` for everything the build could not analyze."""
    return {record.path: record.reason for record in outcome.skipped}


def resolved_name(outcome: BuildOutcome, relpath: str, name: str) -> str | None:
    """The fullname the engine bound `name` to in `relpath`'s module namespace.

    This is the one thing worth asking a raw tree in this task: it is what "the import
    resolved" means, and it is what silently becomes `Any` when the build root is wrong.
    """
    tree = outcome.tree(relpath)
    assert tree is not None, f"no tree for {relpath}"
    symbol = tree.names.get(name)
    node = None if symbol is None else symbol.node
    return None if node is None else node.fullname


# ---------------------------------------------------------------------------
# The pin and the engine's identity (D1, D1a)
# ---------------------------------------------------------------------------


def test_installed_engine_is_the_pinned_version():
    """D1/D1a: the pin is exact, and an upgrade starts by making this test fail."""
    assert ENGINE_VERSION == PINNED_ENGINE_VERSION


def test_engine_meta_names_the_engine_and_its_version(tmp_path):
    """design.md §4.2's `meta.engine` / `meta.engine_version` come from here."""
    root = make_tree(tmp_path / "code", SIBLING_PACKAGE)
    outcome = run(root, tmp_path / "cache", "pkg/__init__.py", "pkg/good.py", "pkg/other.py")
    assert outcome.engine_meta == {"engine": ENGINE_NAME, "engine_version": PINNED_ENGINE_VERSION}


# ---------------------------------------------------------------------------
# Options — the normative §3.5 settings
# ---------------------------------------------------------------------------


def test_build_options_are_the_normative_settings(tmp_path):
    """design.md §3.5's list, item by item, plus D13's no-parallelism."""
    options = build_options(tmp_path / "cache", [tmp_path / "a", tmp_path / "b"])
    assert options.incremental is True
    assert options.cache_dir == str(tmp_path / "cache")
    assert options.export_types is True
    assert options.preserve_asts is True
    assert options.check_untyped_defs is True
    assert options.no_site_packages is True
    assert options.follow_imports == "normal"
    assert options.mypy_path == [str(tmp_path / "a"), str(tmp_path / "b")]
    # D13: the pipeline is sequential; a future change of mypy's default cannot leak
    # workers into a run.
    assert options.num_workers == 0


def test_missing_imports_are_ignored_for_every_module(tmp_path):
    """design.md §3.5's `ignore_missing_imports` for `*`, checked as behavior.

    A literal `"*"` key in `per_module_options` is inert in mypy 2.3.0, so what is
    asserted is the effect the design asks for: whatever module is being checked, an
    absent import is not an error.
    """
    options = build_options(tmp_path / "cache", [tmp_path])
    for module in ("toplevel", "pkg.deep.module"):
        assert options.clone_for_module(module).ignore_missing_imports is True


# ---------------------------------------------------------------------------
# Trap 1 / trap 2 — build root, file root, and sibling resolution
# ---------------------------------------------------------------------------


def test_sibling_imports_resolve_to_the_sibling_definition(tmp_path):
    """Trap 1: the recall-critical property, asserted directly.

    `helper` must be bound to `pkg.other.helper` — the real definition — and not to an
    `Any` placeholder, which is what a build that cannot find the sibling produces
    without emitting any error.
    """
    root = make_tree(tmp_path / "code", SIBLING_PACKAGE)
    outcome = run(root, tmp_path / "cache", "pkg/__init__.py", "pkg/good.py", "pkg/other.py")
    assert resolved_name(outcome, "pkg/good.py", "helper") == "pkg.other.helper"


def test_build_root_is_the_parent_of_an_analyzed_package(tmp_path):
    """Trap 2: file root and build root are different, and the difference is one level.

    The pinned Django benchmark analyzes the `django/` package directory, so the analysis
    root *is* a package: relpaths are package-internal (`other.py`) while module names
    keep the package prefix (`django.other`), and the root the engine searches is the
    analysis root's parent.
    """
    make_tree(tmp_path / "code", SIBLING_PACKAGE)
    root = tmp_path / "code" / "pkg"  # analysis root = the package itself
    outcome = run(root, tmp_path / "cache", "__init__.py", "good.py", "other.py")

    assert {s.relpath: s.module for s in outcome.sources} == {
        "__init__.py": "pkg",
        "good.py": "pkg.good",
        "other.py": "pkg.other",
    }
    assert {s.source_root for s in outcome.sources} == {tmp_path / "code"}
    assert resolved_name(outcome, "good.py", "helper") == "pkg.other.helper"


def test_relpath_derived_module_names_lose_the_import_silently(tmp_path):
    """Trap 1, reproduced in its silent form — the reason this discipline is normative.

    Naming modules from the package-internal relpath (`good`, `other` — the obvious
    shortcut, and what §4.1's node-ID derivation does) leaves the build root one level
    too deep, so `from pkg.other import helper` finds nothing. The engine reports **no
    error**: `helper` becomes a `Var` of type `Any`, the call edge is simply never
    resolvable, and recall drops with nothing to notice. `_crawl_up()` is what prevents
    it, and this test fails if that is ever traded for the simpler rule.
    """
    make_tree(tmp_path / "code", SIBLING_PACKAGE)
    root = tmp_path / "code" / "pkg"
    from mypy.modulefinder import BuildSource  # noqa: PLC0415 - the control needs the raw API

    wrong = [
        BuildSource(str(root / "good.py"), "good", None),
        BuildSource(str(root / "other.py"), "other", None),
    ]
    result = mypy_driver._invoke_build(wrong, build_options(tmp_path / "cache", [root]))

    assert result.errors == []  # silence is the whole problem
    symbol = result.graph["good"].tree.names["helper"]
    assert symbol.node.fullname != "pkg.other.helper"
    assert str(symbol.node.type) == "Any"


def test_extensionless_shebang_scripts_are_analyzed(tmp_path):
    """FR-1 rule (b)'s inputs reach the engine too, named after their file."""
    root = make_tree(
        tmp_path / "code",
        {"bin/tool": "#!/usr/bin/env python3\nimport sys\n\n\ndef main():\n    return sys.argv\n"},
    )
    outcome = run(root, tmp_path / "cache", "bin/tool")
    assert [s.module for s in outcome.sources] == ["tool"]
    assert outcome.tree("bin/tool") is not None
    assert outcome.skipped == ()


# ---------------------------------------------------------------------------
# Trap 3 — the re-extraction set is the rechecked report, never tree presence
# ---------------------------------------------------------------------------


def test_warm_build_with_no_change_rechecks_nothing(tmp_path):
    """Trap 3 / D6 rule 1, the whole of it in one assertion pair.

    After a cold build, a warm build over an unchanged tree re-type-checks nothing. The
    module states are still there — so a re-extraction set inferred from state presence
    would name every file — and this is exactly the inference that silently dropped 8,383
    cached edges in the prototype.
    """
    root = make_tree(tmp_path / "code", SIBLING_PACKAGE)
    cache = tmp_path / "cache"
    relpaths = ("pkg/__init__.py", "pkg/good.py", "pkg/other.py")

    cold = run(root, cache, *relpaths)
    assert {s.module for s in cold.sources} <= cold.rechecked_modules

    warm = run(root, cache, *relpaths)
    assert warm.rechecked_sources() == ()
    assert {s.module for s in warm.sources} & warm.rechecked_modules == set()
    # The states survive the warm build; only the rechecked report tells them apart.
    assert all(warm.state(relpath) is not None for relpath in relpaths)


def test_warm_build_rechecks_only_what_changed(tmp_path):
    """D6 rule 1: a touched leaf puts itself — and not the whole tree — in the report."""
    root = make_tree(tmp_path / "code", SIBLING_PACKAGE)
    cache = tmp_path / "cache"
    relpaths = ("pkg/__init__.py", "pkg/good.py", "pkg/other.py")
    run(root, cache, *relpaths)

    (root / "pkg" / "good.py").write_text(
        "from pkg.other import helper\n\n\ndef top() -> int:\n    return helper() + 1\n",
        encoding="utf-8",
    )
    warm = run(root, cache, *relpaths)
    assert [s.relpath for s in warm.rechecked_sources()] == ["pkg/good.py"]
    assert warm.tree("pkg/good.py") is not None


# ---------------------------------------------------------------------------
# FR-13 — the target codebase is never executed
# ---------------------------------------------------------------------------


WITNESS_FIXTURE = {
    "sideeffect/__init__.py": "",
    "sideeffect/detonator.py": (
        "import pathlib\n"
        "\n"
        "WITNESS = pathlib.Path(__file__).parent.parent / 'witness.txt'\n"
        "\n"
        "WITNESS.write_text('module body executed')\n"
        "\n"
        "\n"
        "def boom():\n"
        "    WITNESS.write_text('boom executed')\n"
        "\n"
        "\n"
        "class Detonator:\n"
        "    def __init__(self):\n"
        "        WITNESS.write_text('constructor executed')\n"
    ),
    "sideeffect/main.py": (
        "from sideeffect.detonator import Detonator, boom\n"
        "\n"
        "\n"
        "def main():\n"
        "    boom()\n"
        "    return Detonator()\n"
    ),
}


def test_analysis_never_executes_the_target_code(tmp_path):
    """AC-13.2 / FR-13, by the `FINDINGS-mypy.md` §FR-13 procedure.

    The fixture writes a witness file from its module body, from a function, and from a
    constructor. Analyzing it must produce no witness — and the analyzed modules must
    never appear in this process's `sys.modules`, since the engine runs in-process and
    an `import` would be the one mechanism that could execute them.
    """
    root = make_tree(tmp_path / "code", WITNESS_FIXTURE)
    before = set(sys.modules)

    outcome = run(
        root,
        tmp_path / "cache",
        "sideeffect/__init__.py",
        "sideeffect/detonator.py",
        "sideeffect/main.py",
    )

    assert outcome.skipped == ()
    assert not (root / "witness.txt").exists()
    assert list(root.glob("**/witness.txt")) == []
    imported = {name for name in set(sys.modules) - before if name.startswith("sideeffect")}
    assert imported == set()
    # The engine still resolved the code it declined to run.
    assert resolved_name(outcome, "sideeffect/main.py", "boom") == "sideeffect.detonator.boom"


def test_analysis_completes_without_the_targets_environment(tmp_path):
    """AC-13.1: missing third-party packages do not stop or degrade the run."""
    absent = "pastapathfinder_absent_dependency"
    assert absent not in sys.modules
    root = make_tree(
        tmp_path / "code",
        {
            "app/__init__.py": "",
            "app/uses_absent.py": (
                f"import {absent}\n"
                "from app.local import here\n"
                "\n"
                "\n"
                "def run():\n"
                f"    {absent}.do()\n"
                "    return here()\n"
            ),
            "app/local.py": "def here() -> int:\n    return 2\n",
        },
    )
    outcome = run(root, tmp_path / "cache", "app/__init__.py", "app/local.py", "app/uses_absent.py")

    assert outcome.skipped == ()
    assert outcome.tree("app/uses_absent.py") is not None
    assert resolved_name(outcome, "app/uses_absent.py", "here") == "app.local.here"


# ---------------------------------------------------------------------------
# FR-6 — per-file failures become skips, and the run continues
# ---------------------------------------------------------------------------


def test_a_syntax_error_skips_one_file_and_analyzes_the_rest(tmp_path):
    """AC-6.1 / EC-1: one unparseable file costs that file and nothing else."""
    root = make_tree(
        tmp_path / "code",
        {
            "pkg/__init__.py": "",
            "pkg/broken.py": "def broken(:\n    pass\n",
            "pkg/fine.py": "def fine() -> int:\n    return 3\n",
        },
    )
    outcome = run(root, tmp_path / "cache", "pkg/__init__.py", "pkg/broken.py", "pkg/fine.py")

    assert skips(outcome) == {"pkg/broken.py": "parse_error"}
    assert outcome.skipped[0].detail  # AC-7.2: a human-readable reason, not just a class
    assert [s.relpath for s in outcome.sources] == ["pkg/__init__.py", "pkg/fine.py"]
    assert outcome.tree("pkg/fine.py") is not None
    assert outcome.tree("pkg/broken.py") is None


def test_a_python2_print_statement_is_a_parse_error(tmp_path):
    """EC-2: Python 2 remnants are a parse failure, not a special case."""
    root = make_tree(tmp_path / "code", {"legacy.py": "print 'hello'\n"})
    outcome = run(root, tmp_path / "cache", "legacy.py")
    assert skips(outcome) == {"legacy.py": "parse_error"}


def test_undecodable_bytes_are_an_encoding_error(tmp_path):
    """EC-12: a non-UTF-8 file with no declaration is skipped as an encoding failure."""
    root = make_tree(tmp_path / "code", {"latin.py": "s = 'é'\n".encode("latin-1")})
    outcome = run(root, tmp_path / "cache", "latin.py")
    assert skips(outcome) == {"latin.py": "encoding_error"}
    assert "decoded" in outcome.skipped[0].detail


def test_a_declared_encoding_is_honoured(tmp_path):
    """EC-12's negative control: a declared encoding is source, not a failure."""
    root = make_tree(
        tmp_path / "code",
        {"latin.py": "# -*- coding: latin-1 -*-\ns = 'é'\n".encode("latin-1")},
    )
    outcome = run(root, tmp_path / "cache", "latin.py")
    assert outcome.skipped == ()
    assert outcome.tree("latin.py") is not None


def test_every_file_failing_still_returns_a_complete_outcome(tmp_path, monkeypatch):
    """AC-6.2: all files unparseable → the run completes, with the engine never called."""

    def refuse(*args, **kwargs):
        raise AssertionError("the engine must not be called when there is nothing to build")

    monkeypatch.setattr(mypy_driver, "_invoke_build", refuse)
    root = make_tree(
        tmp_path / "code",
        {"a.py": "def a(:\n", "b.py": "class B(:\n"},
    )
    outcome = run(root, tmp_path / "cache", "a.py", "b.py")

    assert skips(outcome) == {"a.py": "parse_error", "b.py": "parse_error"}
    assert outcome.sources == ()
    assert outcome.graph == {}
    assert outcome.rechecked_modules == frozenset()
    assert outcome.engine_meta["engine"] == ENGINE_NAME
    # AC-6.2's other half: the run output says so rather than reporting a clean run.
    counts = reports.coverage_counts(discovered=2, analyzed=0, skipped=2, excluded=0)
    rendering = reports.render_coverage(
        reports.coverage_document(
            reports.RunInfo.start().finish(0.0),
            counts,
            [reports.skipped_row(path, "parse error") for path in ("a.py", "b.py")],
        )
    )
    assert "No files were analyzed" in rendering


def test_two_files_claiming_one_module_name_skip_the_loser(tmp_path):
    """A duplicate module name is a per-file failure, never a whole-run one.

    The engine rejects the build outright when one module name maps to two files, so the
    loser is skipped before the build. Order is `relpath` order, so the same file wins on
    every run (FR-44).
    """
    root = make_tree(
        tmp_path / "code",
        {
            "a/mod.py": "def one() -> int:\n    return 1\n",
            "b/mod.py": "def two() -> int:\n    return 2\n",
        },
    )
    outcome = run(root, tmp_path / "cache", "a/mod.py", "b/mod.py")

    assert [s.relpath for s in outcome.sources] == ["a/mod.py"]
    assert skips(outcome) == {"b/mod.py": "engine_error"}
    assert "a/mod.py" in outcome.skipped[0].detail


def test_files_whose_module_names_are_not_identifiers_are_analyzed(tmp_path):
    """Non-identifier module names are ordinary inputs, not failures.

    Both shapes are taken from the pinned Django benchmark, where requiring legal
    identifiers cost 25 of 908 files: every migration is `NNNN_name.py`, and
    `django/conf/locale/is/` is a package whose name is a Python keyword. mypy 2.3.0
    accepts any module-name string, so nothing here needs guarding — only two files
    claiming *the same* name do.
    """
    root = make_tree(
        tmp_path / "code",
        {
            "app/__init__.py": "",
            "app/helper.py": "def h() -> int:\n    return 1\n",
            "app/migrations/__init__.py": "",
            "app/migrations/0001_initial.py": (
                "from app.helper import h\n\n\ndef run():\n    return h()\n"
            ),
            "app/locale/__init__.py": "",
            "app/locale/is/__init__.py": "",
            "app/locale/is/formats.py": "FORMAT = 'x'\n",
        },
    )
    outcome = run(
        root,
        tmp_path / "cache",
        "app/__init__.py",
        "app/helper.py",
        "app/migrations/__init__.py",
        "app/migrations/0001_initial.py",
        "app/locale/__init__.py",
        "app/locale/is/__init__.py",
        "app/locale/is/formats.py",
    )

    assert outcome.skipped == ()
    assert outcome.source_for("app/migrations/0001_initial.py").module == (
        "app.migrations.0001_initial"
    )
    assert outcome.source_for("app/locale/is/formats.py").module == "app.locale.is.formats"
    assert resolved_name(outcome, "app/migrations/0001_initial.py", "h") == "app.helper.h"


@pytest.mark.skipif(AS_ROOT, reason="root can read a mode-000 file")
def test_an_unreadable_file_is_skipped(tmp_path):
    """FR-6: a file that cannot be read costs that file, not the run."""
    root = make_tree(tmp_path / "code", {"secret.py": "x = 1\n", "fine.py": "y = 2\n"})
    (root / "secret.py").chmod(0o000)
    try:
        outcome = run(root, tmp_path / "cache", "secret.py", "fine.py")
    finally:
        (root / "secret.py").chmod(0o644)

    assert skips(outcome) == {"secret.py": "engine_error"}
    assert "secret.py" not in outcome.content_hashes


# ---------------------------------------------------------------------------
# Content hashes and ordering (FR-24's gate, FR-44)
# ---------------------------------------------------------------------------


def test_content_hashes_cover_analyzed_and_skipped_files(tmp_path):
    """The hash of the bytes as read — including for a file that failed to parse."""
    import hashlib

    root = make_tree(tmp_path / "code", {"fine.py": "x = 1\n", "broken.py": "def f(:\n"})
    outcome = run(root, tmp_path / "cache", "fine.py", "broken.py")

    for relpath in ("fine.py", "broken.py"):
        expected = hashlib.sha256((root / relpath).read_bytes()).hexdigest()
        assert outcome.content_hashes[relpath] == expected


def test_sources_and_skips_come_back_in_path_order(tmp_path):
    """FR-44: the outcome does not depend on the order files were handed in."""
    files = {f"pkg/{name}.py": "x = 1\n" for name in ("delta", "alpha", "charlie", "bravo")}
    files["pkg/__init__.py"] = ""
    files["pkg/zbroken.py"] = "def f(:\n"
    files["pkg/abroken.py"] = "def g(:\n"
    root = make_tree(tmp_path / "code", files)

    forward = sorted(files)
    outcome_a = run_build(sources_under(root, *forward), tmp_path / "cache-a", quiet())
    outcome_b = run_build(sources_under(root, *reversed(forward)), tmp_path / "cache-b", quiet())

    assert [s.relpath for s in outcome_a.sources] == sorted(s.relpath for s in outcome_a.sources)
    assert [s.relpath for s in outcome_a.sources] == [s.relpath for s in outcome_b.sources]
    assert [(r.path, r.reason) for r in outcome_a.skipped] == [
        (r.path, r.reason) for r in outcome_b.skipped
    ]
    assert outcome_a.content_hashes == outcome_b.content_hashes


def test_prepare_sources_reports_progress_per_file(tmp_path):
    """AC-41.1: the countable pre-flight phase advances as files are read."""
    root = make_tree(tmp_path / "code", {f"m{i}.py": "x = 1\n" for i in range(3)})
    stream = io.StringIO()
    progress = ProgressSink(stream=stream, interval=0.0)
    prepare_sources(sources_under(root, "m0.py", "m1.py", "m2.py"), progress)
    lines = stream.getvalue().splitlines()
    assert lines[0].endswith("reading sources 0/3")
    assert lines[-1].endswith("reading sources 3/3")


# ---------------------------------------------------------------------------
# The engine seam: fallback, heartbeat, and a result that omits a file
# ---------------------------------------------------------------------------


@dataclass
class FakeState:
    """Stands in for `mypy.build.State` where a real build cannot be steered."""

    tree: Any = None
    abspath: str | None = None
    path: str | None = None


@dataclass
class FakeManager:
    rechecked_modules: set[str]


@dataclass
class FakeResult:
    """The three `BuildResult` members design.md §3.5 permits this module to read."""

    graph: dict[str, FakeState]
    types: dict[object, object]
    manager: FakeManager


def test_a_corrupt_engine_cache_triggers_one_wipe_and_rebuild(tmp_path):
    """AC-24.3, against the real failure: a corrupt cache crashes the whole build.

    mypy 2.3.0 stores its incremental cache in SQLite databases; garbage in them raises
    from inside `build()`. The run must recover by discarding the cache exactly once, say
    so, and still produce the graph.
    """
    root = make_tree(tmp_path / "code", SIBLING_PACKAGE)
    cache = tmp_path / "cache"
    relpaths = ("pkg/__init__.py", "pkg/good.py", "pkg/other.py")
    run(root, cache, *relpaths)

    databases = sorted(cache.rglob("*.db"))
    assert databases, "the engine wrote no cache to corrupt"
    for database in databases:
        database.write_bytes(b"this is not a database" * 8)

    stream = io.StringIO()
    outcome = run(root, cache, *relpaths, progress=ProgressSink(stream=stream))

    assert outcome.cache_fallback is True
    assert FALLBACK_NOTICE in stream.getvalue()  # AC-30.2: never silent
    assert [s.relpath for s in outcome.sources] == list(relpaths)
    assert resolved_name(outcome, "pkg/good.py", "helper") == "pkg.other.helper"


def test_the_fallback_wipes_the_cache_and_rebuilds_exactly_once(tmp_path, monkeypatch):
    """AC-24.3's mechanism, counted: one retry, with a discarded cache, and no loop."""
    root = make_tree(tmp_path / "code", {"m.py": "x = 1\n"})
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "stale").write_text("cache content")

    calls: list[bool] = []

    def flaky(build_sources, options):
        calls.append((cache / "stale").exists())
        if len(calls) == 1:
            raise RuntimeError("engine exploded")
        return FakeResult(
            graph={"m": FakeState(tree=object(), abspath=str(root / "m.py"))},
            types={},
            manager=FakeManager(rechecked_modules={"m"}),
        )

    monkeypatch.setattr(mypy_driver, "_invoke_build", flaky)
    outcome = run(root, cache, "m.py")

    assert calls == [True, False]  # the cache was present, then wiped, then rebuilt
    assert outcome.cache_fallback is True
    assert outcome.rechecked_modules == frozenset({"m"})


def test_a_second_engine_failure_fails_the_run_with_the_engine_error(tmp_path, monkeypatch):
    """design.md §3.5: if the clean-cache rebuild also crashes, the run fails, loudly."""

    def always_fails(build_sources, options):
        raise RuntimeError("engine still exploded")

    monkeypatch.setattr(mypy_driver, "_invoke_build", always_fails)
    root = make_tree(tmp_path / "code", {"m.py": "x = 1\n"})

    with pytest.raises(EngineError) as failure:
        run(root, tmp_path / "cache", "m.py")
    assert "engine still exploded" in str(failure.value)


def test_the_build_phase_emits_a_heartbeat(tmp_path, monkeypatch):
    """AC-41.2: the opaque build phase reports activity rather than falling silent.

    The interval is injected so the bound is measured rather than waited out; the
    production interval is asserted against FR-41's five seconds separately.
    """
    root = make_tree(tmp_path / "code", {"m.py": "x = 1\n"})

    def slow(build_sources, options):
        time.sleep(0.15)
        return FakeResult(
            graph={"m": FakeState(tree=object(), abspath=str(root / "m.py"))},
            types={},
            manager=FakeManager(rechecked_modules={"m"}),
        )

    monkeypatch.setattr(mypy_driver, "_invoke_build", slow)
    stream = io.StringIO()
    run(root, tmp_path / "cache", "m.py", progress=ProgressSink(stream=stream, interval=0.02))

    beats = [line for line in stream.getvalue().splitlines() if PHASE_BUILD in line]
    assert len(beats) >= 2, stream.getvalue()
    assert beats[0].endswith(f"{PHASE_BUILD} … 0s")
    assert PROGRESS_INTERVAL_SECONDS <= 5.0


def test_a_file_the_engine_returns_no_state_for_is_skipped(tmp_path, monkeypatch):
    """FR-6: the engine declining a file is a per-file failure, not a silent omission."""

    def partial(build_sources, options):
        return FakeResult(
            graph={"kept": FakeState(tree=object(), abspath=str(tmp_path / "code" / "kept.py"))},
            types={},
            manager=FakeManager(rechecked_modules={"kept"}),
        )

    monkeypatch.setattr(mypy_driver, "_invoke_build", partial)
    root = make_tree(tmp_path / "code", {"kept.py": "x = 1\n", "dropped.py": "y = 2\n"})
    outcome = run(root, tmp_path / "cache", "kept.py", "dropped.py")

    assert [s.relpath for s in outcome.sources] == ["kept.py"]
    assert skips(outcome) == {"dropped.py": "engine_error"}


def test_a_shadowed_module_name_is_skipped(tmp_path, monkeypatch):
    """A module name that resolved to another file is skipped, never mis-attributed."""

    def shadowed(build_sources, options):
        return FakeResult(
            graph={"m": FakeState(tree=object(), abspath="/somewhere/else/m.py")},
            types={},
            manager=FakeManager(rechecked_modules={"m"}),
        )

    monkeypatch.setattr(mypy_driver, "_invoke_build", shadowed)
    root = make_tree(tmp_path / "code", {"m.py": "x = 1\n"})
    outcome = run(root, tmp_path / "cache", "m.py")

    assert outcome.sources == ()
    assert skips(outcome) == {"m.py": "engine_error"}
    assert "/somewhere/else/m.py" in outcome.skipped[0].detail


# ---------------------------------------------------------------------------
# The outcome's own accessors
# ---------------------------------------------------------------------------


def test_outcome_accessors_answer_by_relpath(tmp_path):
    """`state`/`tree`/`source_for` speak relpaths; unknown paths answer None."""
    root = make_tree(tmp_path / "code", SIBLING_PACKAGE)
    outcome = run(root, tmp_path / "cache", "pkg/__init__.py", "pkg/good.py", "pkg/other.py")

    assert outcome.source_for("pkg/good.py").module == "pkg.good"
    assert outcome.state("pkg/good.py") is not None
    assert outcome.tree("pkg/good.py") is not None
    assert outcome.source_for("pkg/absent.py") is None
    assert outcome.state("pkg/absent.py") is None
    assert outcome.tree("pkg/absent.py") is None


def test_types_map_is_exposed(tmp_path):
    """`BuildResult.types` is what the resolution ladder (task 2.3) will read."""
    root = make_tree(tmp_path / "code", SIBLING_PACKAGE)
    outcome = run(root, tmp_path / "cache", "pkg/__init__.py", "pkg/good.py", "pkg/other.py")
    assert len(outcome.types) > 0
