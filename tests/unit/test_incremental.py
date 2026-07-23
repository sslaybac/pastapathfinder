"""The incremental change gate and evict-and-merge (specs/tasks.md task 4.1).

design.md §3.6 (`incremental`, normative), D6 (the three evict-and-merge rules), D18 (entry
points recomputed wholesale, the metadata gate); requirements FR-24 (all ACs), FR-30,
FR-35, EC-7, EC-13.

Two kinds of test. The unit tests below drive `plan_run`, `merge` and `metadata_hash`
directly, with hand-built indexes and results, so a rule can be checked in isolation. The
end-to-end tests run the real Python adapter through `runner.run_analysis`, because the
decisive property — an incremental update is bit-for-bit the graph a full rebuild would
produce (D6's equivalence proof) — can only be shown against real resolution.
"""

from __future__ import annotations

import io
from dataclasses import replace
from pathlib import Path

from pastapathfinder import incremental, queries, reports, runner
from pastapathfinder.adapters.base import AdapterResult, SourceFile
from pastapathfinder.index import INDEX_FILENAME, full_write, open_index
from pastapathfinder.progress import ProgressSink
from pastapathfinder.schema import (
    META_METADATA_HASH,
    EdgeRow,
    FileRecord,
    FragmentValidationError,
    GraphFragment,
    NodeRow,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def analyze(root: Path, out: Path, **kwargs) -> runner.RunResult:
    """One real-adapter run with captured streams."""
    return runner.run_analysis(
        root,
        out=out,
        progress=ProgressSink(io.StringIO(), interval=0.0),
        stdout=io.StringIO(),
        **kwargs,
    )


def report(result: runner.RunResult, filename: str) -> dict:
    return reports.load_report(result.report_paths[filename])


#: A pyproject declaring one console script; `%s` is the command name.
_PYPROJECT = '[project]\nname = "x"\nversion = "0"\n\n[project.scripts]\n%s = "pkg.app:main"\n'


def content(index_path: Path) -> tuple:
    """Everything in an index except the §5.4 volatile meta — the equivalence surface.

    `created_at` and `run_id` are the only fields two runs over identical input may differ
    in (design.md §5.4); everything else — nodes, edges, files, and the non-volatile meta
    including `metadata_hash` — must match for a merge to equal a rebuild (D6).
    """
    with open_index(index_path) as index:
        connection = index.connection
        nodes = sorted(
            connection.execute(
                "SELECT id, kind, name, language, file_path, start_line, end_line,"
                " is_external, reachable, attrs FROM nodes"
            )
        )
        edges = sorted(
            connection.execute("SELECT src, dst, kind, src_file, is_ambiguous, attrs FROM edges")
        )
        files = sorted(
            connection.execute("SELECT path, content_hash, status, skip_reason FROM files")
        )
        meta = {
            key: value
            for key, value in connection.execute("SELECT key, value FROM meta")
            if key not in ("created_at", "run_id")
        }
    return nodes, edges, files, meta


def edge_pairs(index_path: Path, kind: str = "calls") -> set[tuple[str, str]]:
    """`(src, dst)` for every edge of `kind` — the shape the eviction test asserts on."""
    with open_index(index_path) as index:
        return {
            (str(src), str(dst))
            for src, dst in index.connection.execute(
                "SELECT src, dst FROM edges WHERE kind = ?", (kind,)
            )
        }


def node_ids(index_path: Path) -> set[str]:
    with open_index(index_path) as index:
        return index.node_ids()


# ---------------------------------------------------------------------------
# metadata_hash (D18)
# ---------------------------------------------------------------------------


def test_metadata_hash_is_present_only_and_order_stable(tree):
    """D18: the combined hash covers the three packaging files, present-only, order-stable."""
    root = tree({"pyproject.toml": "[project]\nname='a'\n"})
    only_pyproject = incremental.metadata_hash(root)

    # A tree with none of the three metadata files hashes differently from one with one.
    assert incremental.metadata_hash(tree({"x.py": "\n"}, name="empty")) != only_pyproject

    # Adding a second metadata file moves the hash; the order it is written on disk does not.
    (root / "setup.cfg").write_text("[metadata]\nname = a\n", encoding="utf-8")
    with_two = incremental.metadata_hash(root)
    assert with_two != only_pyproject

    # An absent file contributes nothing: deleting setup.cfg restores the one-file hash.
    (root / "setup.cfg").unlink()
    assert incremental.metadata_hash(root) == only_pyproject


def test_metadata_hash_moves_when_a_metadata_file_changes(tree):
    root = tree({"pyproject.toml": "[project]\nname='a'\n"})
    before = incremental.metadata_hash(root)
    (root / "pyproject.toml").write_text("[project]\nname='b'\n", encoding="utf-8")
    assert incremental.metadata_hash(root) != before


# ---------------------------------------------------------------------------
# plan_run — the change gate (design.md §3.6)
# ---------------------------------------------------------------------------


def _stub_index(tmp_path: Path, tree, files: dict[str, str]):
    """A real index over `files`, built with the stub adapter, plus its `SourceFile`s.

    The stub keeps the engine out of the gate tests: `plan_run` reads the `files` table and
    `meta`, both of which the stub run writes for real.
    """
    from stub_adapter import StubAdapter

    root = tree(files, name=f"gate-{tmp_path.name}")
    out = tmp_path / "out"
    runner.run_analysis(
        root,
        out=out,
        adapters=[StubAdapter()],
        progress=ProgressSink(io.StringIO()),
        stdout=io.StringIO(),
    )
    sources = [SourceFile(path=root / rel, relpath=rel) for rel in files if rel.endswith(".py")]
    return root, out / INDEX_FILENAME, sources


def test_plan_run_sees_no_change_when_nothing_moved(tmp_path, tree):
    """AC-24.1: identical bytes and metadata → the run may take the fast path."""
    root, index_path, sources = _stub_index(
        tmp_path, tree, {"pkg/__init__.py": "", "pkg/a.py": "x = 1\n"}
    )
    with open_index(index_path) as index:
        plan = incremental.plan_run(index, sources)
    assert not plan.has_changes
    assert not plan.needs_engine
    assert plan.changed == frozenset()
    assert plan.metadata_changed is False


def test_plan_run_flags_a_content_change(tmp_path, tree):
    root, index_path, sources = _stub_index(
        tmp_path, tree, {"pkg/__init__.py": "", "pkg/a.py": "x = 1\n"}
    )
    (root / "pkg/a.py").write_text("x = 2\n", encoding="utf-8")
    with open_index(index_path) as index:
        plan = incremental.plan_run(index, sources)
    assert plan.changed == frozenset({"pkg/a.py"})
    assert plan.has_changes and plan.needs_engine
    assert "pkg/__init__.py" in plan.unchanged


def test_plan_run_flags_a_removed_file(tmp_path, tree):
    root, index_path, sources = _stub_index(
        tmp_path, tree, {"pkg/__init__.py": "", "pkg/a.py": "x = 1\n"}
    )
    remaining = [source for source in sources if source.relpath != "pkg/a.py"]
    with open_index(index_path) as index:
        plan = incremental.plan_run(index, remaining)
    assert plan.removed == frozenset({"pkg/a.py"})
    assert plan.has_changes and plan.needs_engine


def test_plan_run_flags_metadata_only_change(tmp_path, tree):
    files = {
        "pkg/__init__.py": "",
        "pkg/a.py": "x = 1\n",
        "pyproject.toml": "[project]\nname='a'\n",
    }
    root, index_path, sources = _stub_index(tmp_path, tree, files)
    (root / "pyproject.toml").write_text("[project]\nname='b'\n", encoding="utf-8")
    with open_index(index_path) as index:
        plan = incremental.plan_run(index, sources)
    # Off the fast path, but no source moved, so no engine pass is needed (D18).
    assert plan.has_changes
    assert plan.metadata_changed is True
    assert plan.changed == frozenset()
    assert not plan.needs_engine


# ---------------------------------------------------------------------------
# merge — evict-and-merge in isolation (D6 rules 2 and 3)
# ---------------------------------------------------------------------------


def _analyzed_file(path: str) -> FileRecord:
    return FileRecord(path=path, content_hash="0" * 64, status="analyzed")


def _fn(module: str, name: str, relpath: str) -> NodeRow:
    return NodeRow(
        id=f"python:{module}.{name}",
        kind="function",
        name=name,
        language="python",
        file_path=relpath,
        start_line=1,
        end_line=1,
    )


def _external(qualified: str) -> NodeRow:
    return NodeRow(
        id=f"python:{qualified}", kind="function", name=qualified, language="python", is_external=1
    )


def _calls(src: str, dst: str, relpath: str) -> EdgeRow:
    return EdgeRow(src=src, dst=dst, kind="calls", src_file=relpath)


def _two_file_index(index_path: Path, root: Path):
    """An index where file a.py calls a private external and a shared one, and b.py the shared.

    a.py: f1 → f2 (internal), f1 → sys.exit (external, a's alone), f1 → os.getcwd (shared).
    b.py: g1 → os.getcwd (shared).
    """
    meta = {
        "tool_version": "t",
        "engine": "stub",
        "engine_version": "0",
        "root_path": str(root),
        "created_at": "x",
        "run_id": "y",
        META_METADATA_HASH: "mh",
    }
    getcwd = _external("os.getcwd")
    fragment_a = GraphFragment(
        file=_analyzed_file("a.py"),
        nodes=[_fn("a", "f1", "a.py"), _fn("a", "f2", "a.py"), _external("sys.exit"), getcwd],
        edges=[
            _calls("python:a.f1", "python:a.f2", "a.py"),
            _calls("python:a.f1", "python:sys.exit", "a.py"),
            _calls("python:a.f1", "python:os.getcwd", "a.py"),
        ],
    )
    fragment_b = GraphFragment(
        file=_analyzed_file("b.py"),
        nodes=[_fn("b", "g1", "b.py"), getcwd],
        edges=[_calls("python:b.g1", "python:os.getcwd", "b.py")],
    )
    with full_write(index_path, meta) as store:
        store.write_fragments([fragment_a, fragment_b])


def test_merge_replaces_and_sweeps_orphaned_externals(tmp_path, tree):
    """D6 rules 2 and 3: a re-extracted file's stale edges go, and its orphaned external too."""
    root = tree({"x.py": "\n"})
    index_path = tmp_path / "index.sqlite"
    _two_file_index(index_path, root)

    # a.py re-derived: f1 now calls only f2 — sys.exit and os.getcwd are no longer called.
    new_a = GraphFragment(
        file=_analyzed_file("a.py"),
        nodes=[_fn("a", "f1", "a.py"), _fn("a", "f2", "a.py")],
        edges=[_calls("python:a.f1", "python:a.f2", "a.py")],
    )
    plan = incremental.RunPlan(changed=frozenset({"a.py"}), metadata_hash="mh2")
    with open_index(index_path, read_only=False) as index:
        merge_report = incremental.merge(index, AdapterResult(fragments=[new_a]), plan)

    ids = node_ids(index_path)
    calls = edge_pairs(index_path)
    # Rule 2 (replace, not union): the two dropped calls are gone, the surviving one stays.
    assert ("python:a.f1", "python:a.f2") in calls
    assert ("python:a.f1", "python:sys.exit") not in calls
    assert ("python:a.f1", "python:os.getcwd") not in calls
    # Rule 3: sys.exit lost its only caller and is swept; os.getcwd is still called by b.py.
    assert "python:sys.exit" not in ids
    assert "python:os.getcwd" in ids
    assert ("python:b.g1", "python:os.getcwd") in calls
    # The metadata hash is refreshed for the next gate, and the attribution is content_changed.
    assert merge_report.reprocessed == (("a.py", "content_changed"),)
    with open_index(index_path) as index:
        assert index.get_meta(META_METADATA_HASH) == "mh2"


def test_merge_evicts_a_removed_file(tmp_path, tree):
    root = tree({"x.py": "\n"})
    index_path = tmp_path / "index.sqlite"
    _two_file_index(index_path, root)

    plan = incremental.RunPlan(removed=frozenset({"b.py"}), metadata_hash="mh")
    with open_index(index_path, read_only=False) as index:
        merge_report = incremental.merge(index, AdapterResult(fragments=[]), plan)

    ids = node_ids(index_path)
    assert "python:b.g1" not in ids
    # os.getcwd was shared; with b.py gone it is still called by a.py, so it survives.
    assert "python:os.getcwd" in ids
    assert merge_report.removed == ("b.py",)


# ---------------------------------------------------------------------------
# End-to-end: the merge equals a full rebuild (the decisive D6 proof)
# ---------------------------------------------------------------------------

EQUIV_TREE = {
    "pkg/__init__.py": "",
    "pkg/base.py": "def base_fn():\n    return 1\n",
    "pkg/leaf.py": "from pkg.base import base_fn\n\n\ndef leaf_fn():\n    return base_fn()\n",
}


def test_incremental_leaf_change_equals_a_full_rebuild(tree, tmp_path):
    """The decisive equivalence proof (D6): warm-merge a leaf edit, diff 0 against a rebuild."""
    root = tree(EQUIV_TREE)
    incremental_out = tmp_path / "incr"
    rebuild_out = tmp_path / "rebuild"

    analyze(root, incremental_out)  # cold build
    # Edit the zero-importer leaf; interface unchanged, so only it is rechecked.
    (root / "pkg/leaf.py").write_text(
        "from pkg.base import base_fn\n\n\ndef leaf_fn():\n    return base_fn() + 1\n",
        encoding="utf-8",
    )
    result = analyze(root, incremental_out)  # warm incremental merge
    assert report(result, reports.REANALYSIS_REPORT)["mode"] == reports.MODE_INCREMENTAL

    analyze(root, rebuild_out, full=True)  # independent cold rebuild of the edited tree

    assert content(incremental_out / INDEX_FILENAME) == content(rebuild_out / INDEX_FILENAME)


def test_incremental_merge_evicts_a_stale_call_edge(tree, tmp_path):
    """The eviction proof: a dropped call is absent, and the graph still equals a rebuild."""
    root = tree(
        {
            "pkg/__init__.py": "",
            "pkg/leaf.py": (
                "def helper():\n    return 1\n\n\ndef caller():\n    return helper()\n"
            ),
        }
    )
    incremental_out = tmp_path / "incr"
    rebuild_out = tmp_path / "rebuild"

    analyze(root, incremental_out)
    before = edge_pairs(incremental_out / INDEX_FILENAME)
    assert ("python:pkg.leaf.caller", "python:pkg.leaf.helper") in before

    (root / "pkg/leaf.py").write_text(
        "def helper():\n    return 1\n\n\ndef caller():\n    return 2\n", encoding="utf-8"
    )
    analyze(root, incremental_out)
    after = edge_pairs(incremental_out / INDEX_FILENAME)
    assert ("python:pkg.leaf.caller", "python:pkg.leaf.helper") not in after

    analyze(root, rebuild_out, full=True)
    assert content(incremental_out / INDEX_FILENAME) == content(rebuild_out / INDEX_FILENAME)


# ---------------------------------------------------------------------------
# End-to-end: reanalysis reporting, the fast path, entry-point recompute, fallback
# ---------------------------------------------------------------------------


def test_no_change_run_takes_the_fast_path(tree, tmp_path):
    """AC-24.1/35.2: a re-run with nothing changed re-processes nothing, index untouched."""
    root = tree(EQUIV_TREE)
    out = tmp_path / "out"
    analyze(root, out)
    before = content(out / INDEX_FILENAME)

    result = analyze(root, out)
    document = report(result, reports.REANALYSIS_REPORT)
    assert document["mode"] == reports.MODE_SKIPPED_NO_CHANGES
    assert document["reprocessed"] == [] and document["removed"] == []
    assert "no files were re-processed" in reports.render_reanalysis(document)
    # The index is byte-for-byte what it was: nothing was re-parsed (AC-24.1).
    assert content(out / INDEX_FILENAME) == before


def test_changed_file_and_its_dependent_are_attributed(tree, tmp_path):
    """AC-24.2/35.1: the changed file is content_changed and its importer a dependent."""
    root = tree(
        {
            "pkg/__init__.py": "",
            "pkg/base.py": "def base_fn():\n    return 1\n",
            "pkg/app.py": "from pkg.base import base_fn\n\n\ndef run():\n    return base_fn()\n",
        }
    )
    out = tmp_path / "out"
    analyze(root, out)

    # Change base's interface (add a parameter) so its importer must be re-resolved.
    (root / "pkg/base.py").write_text("def base_fn(n=0):\n    return n\n", encoding="utf-8")
    result = analyze(root, out)
    document = report(result, reports.REANALYSIS_REPORT)
    assert document["mode"] == reports.MODE_INCREMENTAL
    reprocessed = {row["path"]: row["reason"] for row in document["reprocessed"]}
    assert reprocessed["pkg/base.py"] == "content_changed"
    assert reprocessed["pkg/app.py"] == "dependent"
    # Nothing else was touched: __init__.py did not change and is not a dependent.
    assert "pkg/__init__.py" not in reprocessed


def test_a_removed_source_is_listed_and_evicted(tree, tmp_path):
    """AC-35.3: a file gone from disk is listed as removed and its nodes evicted."""
    root = tree(
        {
            "pkg/__init__.py": "",
            "pkg/keep.py": "def kept():\n    return 1\n",
            "pkg/gone.py": "def leaving():\n    return 1\n",
        }
    )
    out = tmp_path / "out"
    analyze(root, out)
    assert "python:pkg.gone.leaving" in node_ids(out / INDEX_FILENAME)

    (root / "pkg/gone.py").unlink()
    result = analyze(root, out)
    document = report(result, reports.REANALYSIS_REPORT)
    assert document["removed"] == ["pkg/gone.py"]
    assert "python:pkg.gone.leaving" not in node_ids(out / INDEX_FILENAME)


DJANGO_TREE = {
    "app/__init__.py": "",
    "app/urls.py": (
        "from django.urls import path\n\n"
        "from . import views\n\n"
        'urlpatterns = [path("x", views.foo)]\n'
    ),
    "app/views.py": "def foo(request):\n    return 1\n",
}


def test_entry_points_are_recomputed_wholesale(tree, tmp_path):
    """D18/AC-11.3: a deleted routed view leaves no stale entry, and is reported unresolved."""
    root = tree(DJANGO_TREE)
    out = tmp_path / "out"
    analyze(root, out)

    with open_index(out / INDEX_FILENAME) as index:
        entries = queries.entry_points(index)
    assert any(entry.target_id == "python:app.views.foo" for entry in entries)

    # Delete the routed view; the URLconf still references it.
    (root / "app/views.py").write_text("def other(request):\n    return 1\n", encoding="utf-8")
    result = analyze(root, out)

    assert report(result, reports.REANALYSIS_REPORT)["mode"] == reports.MODE_INCREMENTAL
    with open_index(out / INDEX_FILENAME) as index:
        entries = queries.entry_points(index)
    # The stale entry targeting the deleted view is gone (wholesale recompute).
    assert not any(entry.target_id == "python:app.views.foo" for entry in entries)
    # The dangling route is recorded, not silently dropped (AC-11.3).
    kinds = {row["kind"] for row in report(result, reports.DIAGNOSTICS_REPORT)["diagnostics"]}
    assert "unresolved_entry_declaration" in kinds


def test_metadata_only_change_refreshes_console_scripts(tree, tmp_path):
    """D18: editing pyproject alone leaves the fast path and re-detects console scripts."""
    root = tree(
        {
            "pkg/__init__.py": "",
            "pkg/app.py": "def main():\n    return 0\n",
            "pyproject.toml": _PYPROJECT % "mycli",
        }
    )
    out = tmp_path / "out"
    analyze(root, out)
    with open_index(out / INDEX_FILENAME) as index:
        assert [entry.name for entry in queries.entry_points(index)] == ["mycli"]

    # A no-source, no-metadata re-run is the fast path.
    assert (
        report(analyze(root, out), reports.REANALYSIS_REPORT)["mode"]
        == reports.MODE_SKIPPED_NO_CHANGES
    )

    # Rename the script — a metadata-only change — and the entry point is refreshed.
    (root / "pyproject.toml").write_text(_PYPROJECT % "second", encoding="utf-8")
    result = analyze(root, out)
    assert report(result, reports.REANALYSIS_REPORT)["mode"] == reports.MODE_INCREMENTAL
    with open_index(out / INDEX_FILENAME) as index:
        assert [entry.name for entry in queries.entry_points(index)] == ["second"]


def test_a_merge_validation_failure_falls_back_to_a_full_rebuild(tree, tmp_path, monkeypatch):
    """AC-24.3/35.4/30.2: a fragment the merge cannot apply triggers a full cache-fallback."""
    root = tree(EQUIV_TREE)
    out = tmp_path / "out"
    analyze(root, out)

    (root / "pkg/leaf.py").write_text(
        "from pkg.base import base_fn\n\n\ndef leaf_fn():\n    return base_fn() + 7\n",
        encoding="utf-8",
    )

    # Force the merge to reject: the first `write_fragments` on the incremental path raises a
    # validation error, exactly as a corrupt fragment would (design.md §3.6's fallback trigger).
    original = incremental.Index.write_fragments
    calls = {"n": 0}

    def flaky(self, fragments):
        calls["n"] += 1
        if calls["n"] == 1:
            raise FragmentValidationError("injected: corrupt merge input")
        return original(self, fragments)

    monkeypatch.setattr(incremental.Index, "write_fragments", flaky)
    stderr = io.StringIO()
    result = runner.run_analysis(
        root, out=out, progress=ProgressSink(stderr, interval=0.0), stdout=io.StringIO()
    )

    document = report(result, reports.REANALYSIS_REPORT)
    assert document["mode"] == reports.MODE_FALLBACK
    # Every file is attributed cache_fallback (AC-35.4), and the fallback still produced a
    # correct index: it equals a clean rebuild of the edited tree.
    reasons = {row["reason"] for row in document["reprocessed"]}
    assert reasons == {"cache_fallback"}
    assert {row["path"] for row in document["reprocessed"]} >= {"pkg/base.py", "pkg/leaf.py"}
    # AC-30.2: the user is told a longer run is under way, while it runs.
    assert "running a full analysis" in stderr.getvalue()

    monkeypatch.undo()
    rebuild_out = tmp_path / "rebuild"
    analyze(root, rebuild_out, full=True)
    assert content(out / INDEX_FILENAME) == content(rebuild_out / INDEX_FILENAME)


def test_an_adapter_cache_fallback_is_published_as_a_full_fallback(tree, tmp_path):
    """AC-24.3: an adapter that had to rebuild cold is published as a fallback, not a merge."""
    from stub_adapter import StubAdapter

    class ColdRebuildStub(StubAdapter):
        def analyze(self, root, files, cache_dir, changed, progress, prior_nodes=None):
            result = super().analyze(root, files, cache_dir, changed, progress, prior_nodes)
            # Simulate the adapter's own wipe-and-rebuild recovery (mypy_driver, AC-24.3):
            # the result is complete, and it flags that a cold rebuild happened.
            return replace(result, cache_fallback=True)

    root = tree({"pkg/__init__.py": "", "pkg/a.py": "x = 1\n"})
    out = tmp_path / "out"
    adapter = ColdRebuildStub()

    def run():
        return runner.run_analysis(
            root,
            out=out,
            adapters=[adapter],
            progress=ProgressSink(io.StringIO()),
            stdout=io.StringIO(),
        )

    run()  # first run: a plain full build
    (root / "pkg/a.py").write_text("x = 2\n", encoding="utf-8")  # force an incremental attempt
    result = run()

    document = report(result, reports.REANALYSIS_REPORT)
    assert document["mode"] == reports.MODE_FALLBACK
    assert {row["reason"] for row in document["reprocessed"]} == {"cache_fallback"}
