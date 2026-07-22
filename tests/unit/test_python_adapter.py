"""The Python adapter in the real run path (specs/tasks.md task 2.5).

design.md §1 (data flow), §3.4 (the protocol), §3.5 (the submodules it composes), §3.10
(`runner`), §7; requirements FR-6 (AC-6.1, AC-6.2), FR-7 (AC-7.1, AC-7.2), FR-12, FR-13
(AC-13.2), FR-23 (AC-23.1, AC-23.2), FR-36, FR-41, FR-43, EC-12.

Milestone 1's walking skeleton ran on a test double; these tests run `analyze` with the
registry the shipped build uses, over real trees, with the real engine — so what is under
test is the *wiring*: that the four submodules of design.md §3.5 compose into one fragment
per file, that the store accepts what they produce, and that the run's accounting still
reconciles when the engine declines a file.

Every fixture here is a few files, because the engine build dominates the runtime. The
codebase-scale evidence — FR-29 timing, the coverage reconciliation over 908 files — lives
in `test_analyze_benchmark.py`, which is opt-in against the pinned checkout.
"""

from __future__ import annotations

import io
import os
import sqlite3
import stat
from pathlib import Path

import pytest

from conftest import write_tree
from pastapathfinder import cli, reports, runner
from pastapathfinder.adapters.base import LanguageAdapter, SourceFile
from pastapathfinder.adapters.python import PythonAdapter
from pastapathfinder.index import open_index
from pastapathfinder.progress import ProgressSink

# A two-module package with one cross-file call, one method call through an instance, one
# call into the standard library, and a `__main__` block — the shapes the ladder resolves
# through different rungs.
SMALL_TREE = {
    "pkg/__init__.py": "",
    "pkg/util.py": "def helper(name):\n    return name.upper()\n",
    "pkg/app.py": (
        "import os\n"
        "\n"
        "from pkg.util import helper\n"
        "\n"
        "\n"
        "class Greeter:\n"
        "    def greet(self, name):\n"
        "        return helper(name)\n"
        "\n"
        "\n"
        "def main():\n"
        "    os.getcwd()\n"
        "    return Greeter().greet('world')\n"
        "\n"
        "\n"
        'if __name__ == "__main__":\n'
        "    main()\n"
    ),
}


def analyze(root: Path, out: Path, **kwargs):
    """One run through the real registry, with both output streams captured."""
    stdout = io.StringIO()
    stderr = io.StringIO()
    result = runner.run_analysis(
        root,
        out=out,
        progress=ProgressSink(stderr, interval=0.0),
        stdout=stdout,
        **kwargs,
    )
    return result, stdout.getvalue(), stderr.getvalue()


@pytest.fixture(scope="module")
def small_run(tmp_path_factory):
    """One analysis of `SMALL_TREE`, shared by the assertions that only read its output.

    Module-scoped deliberately: every run here drives a real cold engine build, so tests
    that ask different questions of the *same* run should not each pay for one.
    """
    base = tmp_path_factory.mktemp("small")
    root = write_tree(base / "codebase", SMALL_TREE)
    out = base / "out"
    out.mkdir()
    return analyze(root, out)


def report(result: runner.RunResult, filename: str) -> dict:
    return reports.load_report(result.report_paths[filename])


def rows(index_path: Path, sql: str) -> list[tuple]:
    connection = sqlite3.connect(index_path)
    try:
        return list(connection.execute(sql))
    finally:
        connection.close()


def node_ids(index_path: Path) -> set[str]:
    return {row[0] for row in rows(index_path, "SELECT id FROM nodes")}


def call_edges(index_path: Path) -> set[tuple[str, str]]:
    return {
        (row[0], row[1])
        for row in rows(index_path, "SELECT src, dst FROM edges WHERE kind = 'calls'")
    }


# ---------------------------------------------------------------------------
# The seam (FR-23, design.md §3.4)
# ---------------------------------------------------------------------------


def test_the_adapter_satisfies_the_protocol():
    """FR-23: the shipped adapter implements §3.4's protocol rather than resembling it."""
    assert isinstance(PythonAdapter(), LanguageAdapter)
    assert PythonAdapter().language == "python"


@pytest.mark.parametrize(
    ("name", "first_line", "expected"),
    [
        ("mod.py", None, True),
        ("notes.txt", None, False),
        ("data.json", b"#!/usr/bin/env python3", False),
        ("runme", b"#!/usr/bin/env python3", True),
        ("runme", b"#!/bin/sh", False),
        ("runme", None, False),
    ],
)
def test_recognizes_follows_fr1s_two_rules(name, first_line, expected):
    """FR-1 (a)/(b): a `.py` suffix, or an extensionless file with a Python shebang."""
    assert PythonAdapter().recognizes(Path("/tree") / name, first_line) is expected


# ---------------------------------------------------------------------------
# The populated index (FR-12, FR-36, design.md §1)
# ---------------------------------------------------------------------------


def test_a_real_run_populates_the_index_with_the_call_graph(small_run):
    """FR-12/FR-36 through the shipped path: the graph reaches the store, not just memory."""
    result, _, _ = small_run

    assert result.completed
    ids = node_ids(result.index_path)
    # One module-body node per analyzed file (D16), the definitions, and the file nodes.
    assert {
        "python:file:pkg/app.py",
        "python:pkg.app.<module>",
        "python:pkg.app.Greeter",
        "python:pkg.app.Greeter.greet",
        "python:pkg.app.main",
        "python:pkg.util.helper",
    } <= ids

    edges = call_edges(result.index_path)
    # AC-12.1: an unambiguous cross-file call, and a method reached through an instance.
    assert ("python:pkg.app.Greeter.greet", "python:pkg.util.helper") in edges
    assert ("python:pkg.app.main", "python:pkg.app.Greeter.greet") in edges
    # D16: a top-level call attaches to the module node, never to the file node.
    assert ("python:pkg.app.<module>", "python:pkg.app.main") in edges
    assert not any(src.startswith("python:file:") for src, _ in edges)

    # AC-36.1: the standard-library call is a leaf marked external, with no location.
    external = rows(
        result.index_path,
        "SELECT id, file_path, start_line FROM nodes WHERE is_external = 1",
    )
    assert ("python:os.getcwd", None, None) in external
    # AC-36.3's precondition, enforced: an external leaf never has an outgoing edge.
    externals = {row[0] for row in external}
    assert not {src for src, _ in edges} & externals

    # `contains` and `imports` edges reached the store too (FR-12's node half, FR-35's
    # attribution input).
    structural = rows(result.index_path, "SELECT src, dst FROM edges WHERE kind = 'imports'")
    assert ("python:file:pkg/app.py", "python:file:pkg/util.py") in structural


def test_the_index_names_the_engine_the_adapter_reported(small_run):
    """design.md §4.2's provenance keys come from the adapter, never from the pipeline."""
    result, _, _ = small_run
    with open_index(result.index_path) as index:
        assert index.get_meta("engine") == "mypy"
        assert index.get_meta("engine_version") == "2.3.0"


def test_target_code_is_never_executed(tree, out_dir):
    """AC-13.2 at the wiring level: analysis of a side-effecting module writes no witness."""
    witness = out_dir / "witness.txt"
    root = tree(
        {
            "pkg/__init__.py": "",
            "pkg/detonator.py": (
                "from pathlib import Path\n"
                "\n"
                f"Path({str(witness)!r}).write_text('module body ran')\n"
                "\n"
                "\n"
                "def boom():\n"
                f"    Path({str(witness)!r}).write_text('boom ran')\n"
            ),
        }
    )
    analyze(root, out_dir)
    assert not witness.exists()
    assert "pkg.detonator" not in list(__import__("sys").modules)


def test_a_second_run_over_an_unchanged_tree_reproduces_the_graph(tree, out_dir):
    """The D6-rule-1 trap, at the level where it would do the damage — and FR-44's shape.

    mypy re-type-checks nothing when nothing changed, so a second build over a warm cache
    hands back no trees and no types. Every run being a full analysis (task 4.1 owns the
    incremental path) means the second index must be as complete as the first — an index
    that quietly lost its edges is exactly the failure `FINDINGS-session5.md` Part 1
    documents. Row-for-row equality is also the gross-instability check task 2.5 asks for;
    the formal FR-44 comparator gate arrives in task 4.3.
    """
    root = tree(SMALL_TREE)
    first, _, _ = analyze(root, out_dir)
    before = (
        rows(first.index_path, "SELECT * FROM nodes ORDER BY id"),
        rows(first.index_path, "SELECT * FROM edges ORDER BY src, dst, kind"),
    )
    assert before[1]  # a graph with edges, so "identical" is not vacuous
    assert (out_dir / runner.CACHE_DIRNAME).is_dir()  # the cache was written…

    second, _, _ = analyze(root, out_dir)  # …and the next run does not half-use it
    after = (
        rows(second.index_path, "SELECT * FROM nodes ORDER BY id"),
        rows(second.index_path, "SELECT * FROM edges ORDER BY src, dst, kind"),
    )
    assert after == before


# ---------------------------------------------------------------------------
# Partial analysis (FR-6, FR-7, EC-12)
# ---------------------------------------------------------------------------


def test_one_unparseable_file_is_skipped_and_the_rest_are_analyzed(tree, out_dir):
    """AC-6.1: the run completes, the offender is a `parse_error` skip with a reason."""
    root = tree({**SMALL_TREE, "pkg/broken.py": "def oops(:\n"})
    result, stdout, _ = analyze(root, out_dir)

    coverage = {row["path"]: row for row in report(result, reports.COVERAGE_REPORT)["files"]}
    assert coverage["pkg/broken.py"]["status"] == "skipped"
    assert "parsed" in coverage["pkg/broken.py"]["reason"]  # AC-7.2, in words
    assert coverage["pkg/app.py"]["status"] == "analyzed"
    assert result.counts["files_analyzed"] == 3
    assert result.counts["files_skipped"] == 1

    with open_index(result.index_path) as index:
        assert index.content_hashes()["pkg/broken.py"]  # the files row still exists
    assert cli.exit_code_for(result) == cli.EXIT_PARTIAL  # AC-43.2
    assert "pkg/broken.py" in stdout


def test_a_non_utf8_file_is_an_encoding_skip(tree, out_dir):
    """EC-12: undecodable source is a per-file failure with an encoding reason."""
    root = tree(SMALL_TREE)
    (root / "pkg" / "latin.py").write_bytes(b"# caf\xe9\nx = 1\n")
    result, _, _ = analyze(root, out_dir)
    coverage = {row["path"]: row for row in report(result, reports.COVERAGE_REPORT)["files"]}
    assert coverage["pkg/latin.py"]["status"] == "skipped"
    assert "decoded" in coverage["pkg/latin.py"]["reason"]
    assert rows(result.index_path, "SELECT skip_reason FROM files WHERE path = 'pkg/latin.py'") == [
        ("encoding_error",)
    ]


def test_a_tree_in_which_every_file_fails_still_completes_and_says_so(tree, out_dir):
    """AC-6.2: an index and reports reflecting zero analyzed files, stated explicitly."""
    root = tree({"a.py": "def a(:\n", "b.py": "def b(:\n"})
    result, stdout, _ = analyze(root, out_dir)

    assert result.completed
    assert result.index_path.is_file()
    assert result.counts["files_analyzed"] == 0
    assert result.counts["files_skipped"] == 2
    assert "No files were analyzed: every discovered source was skipped." in stdout
    assert node_ids(result.index_path) == set()
    assert cli.exit_code_for(result) == cli.EXIT_PARTIAL


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_an_unreadable_candidate_is_counted_as_skipped(tree, out_dir):
    """FR-6/AC-7.1: a file with no readable bytes has no `files` row and is still counted.

    It is the one input that yields a `SkipRecord` and no fragment — there is no content
    to hash — so it is the case that proves the runner reconciles from both halves of the
    adapter's result rather than from fragments alone.
    """
    root = tree({**SMALL_TREE, "pkg/locked.py": "x = 1\n"})
    locked = root / "pkg" / "locked.py"
    locked.chmod(0)
    try:
        result, _, _ = analyze(root, out_dir)
    finally:
        locked.chmod(stat.S_IRUSR | stat.S_IWUSR)

    counts = report(result, reports.COVERAGE_REPORT)["counts"]
    assert (
        counts["entries_discovered"]
        == counts["files_analyzed"] + counts["files_skipped"] + counts["entries_excluded"]
    )
    coverage = {row["path"]: row for row in report(result, reports.COVERAGE_REPORT)["files"]}
    assert coverage["pkg/locked.py"]["status"] == "skipped"
    assert "read" in coverage["pkg/locked.py"]["reason"]
    assert "pkg/locked.py" not in dict(rows(result.index_path, "SELECT path, 1 FROM files"))


def test_coverage_reconciles_across_analyzed_skipped_and_excluded(tree, out_dir):
    """AC-7.1/AC-42.2 over a tree with one of each, computed from the counts alone."""
    root = tree({**SMALL_TREE, "pkg/broken.py": "def oops(:\n", "build/gen.py": "x = 1\n"})
    result, _, _ = analyze(root, out_dir)
    counts = report(result, reports.COVERAGE_REPORT)["counts"]
    assert counts == {
        "entries_discovered": 5,
        "files_analyzed": 3,
        "files_skipped": 1,
        "entries_excluded": 1,
    }


# ---------------------------------------------------------------------------
# Diagnostics and progress (C-10, FR-41)
# ---------------------------------------------------------------------------


def test_unresolved_call_sites_reach_the_diagnostics_report(tree, out_dir):
    """AC-14.2 through the run: the C-11 gap is auditable in `diagnostics.json`."""
    root = tree(
        {
            "pkg/__init__.py": "",
            "pkg/dynamic.py": (
                "def dispatch(handler, name):\n    return getattr(handler, name)()\n"
            ),
        }
    )
    result, _, _ = analyze(root, out_dir)
    diagnostics = report(result, reports.DIAGNOSTICS_REPORT)["diagnostics"]
    unresolved = [row for row in diagnostics if row["kind"] == "unresolved_call"]
    assert unresolved
    assert all(row["path"] == "pkg/dynamic.py" for row in unresolved)
    assert all(row["extra"]["callee"] for row in unresolved)


def test_the_adapters_phases_reach_the_progress_channel(small_run):
    """AC-41.1/41.2: the countable phases and the opaque build's heartbeat, on stderr."""
    _, stdout, stderr = small_run
    assert "reading sources 3/3" in stderr
    assert "analyzing (engine build) …" in stderr
    assert "extracting definitions 3/3" in stderr
    assert "resolving calls 3/3" in stderr
    assert "extracting definitions" not in stdout


# ---------------------------------------------------------------------------
# What the adapter hands back (design.md §3.4)
# ---------------------------------------------------------------------------


def test_the_result_reports_every_file_it_re_derived(tree, out_dir):
    """`rechecked` is the re-extraction set: every analyzed file, since every run is full."""
    root = tree(SMALL_TREE)
    files = [
        SourceFile(path=root / relpath, relpath=relpath)
        for relpath in sorted(SMALL_TREE)
        if relpath.endswith(".py")
    ]
    result = PythonAdapter().analyze(
        root=root,
        files=files,
        cache_dir=out_dir / "cache",
        changed=None,
        progress=ProgressSink(io.StringIO(), interval=0.0),
    )
    assert result.rechecked == {source.path for source in files}
    assert [fragment.file.path for fragment in result.fragments] == sorted(
        source.relpath for source in files
    )
    assert result.engine_meta == {"engine": "mypy", "engine_version": "2.3.0"}
