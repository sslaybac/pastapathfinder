"""The FR-44 comparator's own unit tests (specs/tasks.md task 4.3).

design.md §3.10 (the comparator's normative classification), §5.4 (the volatile-field
register), D12; requirements FR-44.

The comparator is the instrument the determinism gate reads, so it is calibrated here
before it is trusted: it must strip *exactly* the §5.4 volatile fields and nothing else,
call the documented engine-variance class by its name (a warning, never a silent pass),
and refuse to absorb anything outside that class however small.

The variance-class tests need a realistic denominator — 0.001 % of the call edges is one
edge only when there are a hundred thousand of them — so this module builds one synthetic
100,000-edge index once and derives its variants from it by deleting rows.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import compare
import pytest

from pastapathfinder import reports
from pastapathfinder.index import INDEX_FILENAME, full_write
from pastapathfinder.schema import EdgeRow, FileRecord, NodeRow

# ---------------------------------------------------------------------------
# Index fixtures
# ---------------------------------------------------------------------------

#: The required §4.2 `meta`, including the volatile pair this comparator must strip.
META = {
    "tool_version": "1.2.3",
    "engine": "mypy",
    "engine_version": "2.3.0",
    "root_path": "/codebase",
    "created_at": "2026-07-24T09:00:00+00:00",
    "run_id": "run-left",
}

HASH_A = "a" * 64
HASH_B = "b" * 64


def function(name: str, line: int = 1) -> NodeRow:
    return NodeRow(
        id=f"python:pkg.app.{name}",
        kind="function",
        name=name,
        language="python",
        file_path="pkg/app.py",
        start_line=line,
        end_line=line + 2,
    )


def external(qualname: str) -> NodeRow:
    return NodeRow(
        id=f"python:{qualname}", kind="function", name=qualname, language="python", is_external=1
    )


def call(src: NodeRow, dst: NodeRow, sites: tuple[tuple[int, int], ...] = ((3, 4),)) -> EdgeRow:
    return EdgeRow(
        src=src.id,
        dst=dst.id,
        kind="calls",
        src_file="pkg/app.py",
        attrs={"call_sites": [list(site) for site in sites]},
    )


def write_index(path: Path, nodes, edges, *, files=(), meta=None) -> Path:
    """A real index over hand-built rows, through the product's own write path."""
    with full_write(path, {**META, **(meta or {})}) as store:
        if files:
            store.write_files(files)
        store.write_rows(list(nodes), list(edges))
    return path


def variant(base: Path, target: Path, *, drop_edges=(), drop_nodes=()) -> Path:
    """A copy of `base` with rows removed or added directly, bypassing validation.

    Deliberately raw SQL: some of the states a comparator must judge — a node that vanished
    while an edge to it did not — are states the validating write path exists to prevent.
    """
    shutil.copy(base, target)
    connection = sqlite3.connect(target)
    try:
        connection.executemany(
            "DELETE FROM edges WHERE src = ? AND dst = ? AND kind = ?", list(drop_edges)
        )
        connection.executemany("DELETE FROM nodes WHERE id = ?", [(node,) for node in drop_nodes])
        connection.commit()
    finally:
        connection.close()
    return target


# A three-function graph with one external leaf: the smallest index that has one of
# everything the classification distinguishes.
CALLER = function("caller", line=1)
CALLEE = function("callee", line=10)
LONELY = function("lonely", line=20)
EXTERNAL = external("json.dumps")
SMALL_NODES = (CALLER, CALLEE, LONELY, EXTERNAL)
SMALL_EDGES = (
    call(CALLER, CALLEE),
    call(CALLER, EXTERNAL),
    # A second caller of the same external leaf: AC-36.5's dedup shape, and what makes the
    # "referenced only by those edges" test below have something to fail on.
    call(CALLEE, EXTERNAL),
    EdgeRow(src=CALLER.id, dst=CALLEE.id, kind="contains", src_file="pkg/app.py"),
)
SMALL_FILES = (FileRecord(path="pkg/app.py", content_hash=HASH_A, status="analyzed"),)


@pytest.fixture
def small(tmp_path: Path) -> Path:
    return write_index(tmp_path / "left.sqlite", SMALL_NODES, SMALL_EDGES, files=SMALL_FILES)


# ---------------------------------------------------------------------------
# The volatile strip: exactly §5.4's fields, and nothing else
# ---------------------------------------------------------------------------


def test_two_indexes_written_in_different_orders_compare_equal(tmp_path: Path, small: Path):
    """The FR-44 write discipline seen from the reading end: order carries no information."""
    right = write_index(
        tmp_path / "right.sqlite",
        reversed(SMALL_NODES),
        reversed(SMALL_EDGES),
        files=SMALL_FILES,
    )

    result = compare.compare_indexes(small, right)

    assert result.verdict == compare.EQUAL
    assert result.differences == ()
    assert result.equal and result.ok


def test_the_volatile_meta_pair_is_stripped(tmp_path: Path, small: Path):
    """§5.4: `meta.created_at` and `meta.run_id` are the index's only volatile content."""
    right = write_index(
        tmp_path / "right.sqlite",
        SMALL_NODES,
        SMALL_EDGES,
        files=SMALL_FILES,
        meta={"created_at": "2026-07-24T17:30:00+00:00", "run_id": "run-right"},
    )

    # The two files genuinely differ — the equality below is the strip working, not two
    # identical files trivially matching.
    assert small.read_bytes() != right.read_bytes()
    assert compare.compare_indexes(small, right).verdict == compare.EQUAL


@pytest.mark.parametrize(
    "key, value",
    [
        ("tool_version", "9.9.9"),
        ("engine_version", "2.4.0"),
        ("root_path", "/elsewhere"),
    ],
)
def test_a_non_volatile_meta_difference_is_a_defect(tmp_path: Path, small: Path, key, value):
    """ "and nothing else": every other `meta` key is compared (FR-44)."""
    right = write_index(
        tmp_path / "right.sqlite", SMALL_NODES, SMALL_EDGES, files=SMALL_FILES, meta={key: value}
    )

    result = compare.compare_indexes(small, right)

    assert result.verdict == compare.DEFECT
    assert not result.ok
    assert [difference.key for difference in result.differences] == [key]
    assert key in result.summary()


def test_a_files_row_difference_is_a_defect(tmp_path: Path, small: Path):
    """A file whose recorded content hash moved is not a variance, it is a different run."""
    right = write_index(
        tmp_path / "right.sqlite",
        SMALL_NODES,
        SMALL_EDGES,
        files=(FileRecord(path="pkg/app.py", content_hash=HASH_B, status="analyzed"),),
    )

    result = compare.compare_indexes(small, right)

    assert result.verdict == compare.DEFECT
    assert [difference.category for difference in result.differences] == [compare.CATEGORY_FILES]


# ---------------------------------------------------------------------------
# The variance class: shape (this section), then threshold (the next)
# ---------------------------------------------------------------------------


def test_an_internal_node_difference_is_a_defect(tmp_path: Path, small: Path):
    """A missing *function* node is never engine variance, however few edges it costs."""
    right = variant(
        small,
        tmp_path / "right.sqlite",
        drop_nodes=(LONELY.id,),
    )

    result = compare.compare_indexes(small, right)

    assert result.verdict == compare.DEFECT
    assert result.variance == ()
    assert LONELY.id in result.summary()


def test_a_non_call_edge_difference_is_a_defect(tmp_path: Path, small: Path):
    """The class covers `calls` edges only: a missing `contains` edge is structural loss."""
    right = variant(
        small, tmp_path / "right.sqlite", drop_edges=((CALLER.id, CALLEE.id, "contains"),)
    )

    result = compare.compare_indexes(small, right)

    assert result.verdict == compare.DEFECT
    assert result.variance == ()


def test_a_call_edge_whose_attrs_moved_is_a_defect(tmp_path: Path, small: Path):
    """Presence/absence is the class. An edge present on both sides that *changed* is not."""
    right = write_index(
        tmp_path / "right.sqlite",
        SMALL_NODES,
        (call(CALLER, CALLEE, sites=((3, 4), (9, 12))), *SMALL_EDGES[1:]),
        files=SMALL_FILES,
    )

    result = compare.compare_indexes(small, right)

    assert result.verdict == compare.DEFECT
    assert result.variance == ()
    assert result.differences[0].present_only_in is None


def test_an_external_node_still_referenced_elsewhere_is_not_in_class(tmp_path: Path, small: Path):
    """ "referenced only by those edges" is checked, not assumed.

    On the left the external leaf has two callers; the right-hand index drops the leaf and
    only *one* of them, so the leaf's other reference is an edge both sides still hold. The
    node's disappearance is therefore not explained by the varying edge, and it is a defect
    even though the edge difference beside it is in class. (The right-hand index is
    deliberately inconsistent — an edge whose target is gone — because that is the state the
    containment test exists to catch; the validating write path would refuse to create it.)
    """
    right = variant(
        small,
        tmp_path / "right.sqlite",
        drop_edges=((CALLER.id, EXTERNAL.id, "calls"),),
        drop_nodes=(EXTERNAL.id,),
    )

    result = compare.compare_indexes(small, right)

    assert result.verdict == compare.DEFECT
    # The call-edge difference is in class by shape; the external node it left behind is not.
    assert [difference.category for difference in result.variance] == [compare.CATEGORY_EDGES]
    assert [difference.category for difference in result.defects] == [compare.CATEGORY_NODES]


# ---------------------------------------------------------------------------
# The variance class: the 0.01 % threshold, at a realistic denominator
# ---------------------------------------------------------------------------

#: Exactly 100,000 call edges, so one differing edge is 0.001 % — the scale at which the
#: measured pandas variance (3 of 88,228) is one thousandth of a percent per edge.
CALL_EDGES = 100_000
CHAIN = CALL_EDGES - 1
LEAF = "python:pandas.core.computation.ops._in"


@pytest.fixture(scope="module")
def large(tmp_path_factory) -> Path:
    """One 100,000-call-edge index, built once; the variants below are copies of it."""
    nodes = [
        NodeRow(
            id=f"python:pkg.mod.f{position}",
            kind="function",
            name=f"f{position}",
            language="python",
            file_path="pkg/mod.py",
            start_line=position + 1,
            end_line=position + 1,
        )
        for position in range(CHAIN + 1)
    ]
    edges = [
        EdgeRow(
            src=nodes[position].id, dst=nodes[position + 1].id, kind="calls", src_file="pkg/mod.py"
        )
        for position in range(CHAIN)
    ]
    leaf = NodeRow(id=LEAF, kind="function", name=LEAF, language="python", is_external=1)
    edges.append(EdgeRow(src=nodes[0].id, dst=leaf.id, kind="calls", src_file="pkg/mod.py"))
    path = tmp_path_factory.mktemp("large") / "index.sqlite"
    return write_index(path, [*nodes, leaf], edges)


def chain_edges(count: int, start: int = 0):
    """`(src, dst, kind)` triples for `count` consecutive edges of the synthetic chain."""
    return [
        (f"python:pkg.mod.f{position}", f"python:pkg.mod.f{position + 1}", "calls")
        for position in range(start, start + count)
    ]


def test_one_missing_call_edge_is_in_variance_class(tmp_path: Path, large: Path):
    """0.001 % of the call edges: reported as a warning, not failed (FR-44 amendment)."""
    right = variant(large, tmp_path / "right.sqlite", drop_edges=chain_edges(1, start=5))

    with pytest.warns(compare.VarianceWarning) as caught:
        result = compare.compare_indexes(large, right)

    assert result.verdict == compare.IN_VARIANCE_CLASS
    assert result.ok and not result.equal
    assert result.call_edges == CALL_EDGES
    assert result.variance_fraction == pytest.approx(0.00001)
    assert len(result.differences) == 1 and len(result.variance) == 1
    # "reported, never silently passed": the warning carries the account, not just a flag.
    assert "in variance class" in str(caught[0].message)
    assert "python:pkg.mod.f5" in str(caught[0].message)


def test_an_external_leaf_orphaned_by_a_variance_edge_is_in_class(tmp_path: Path, large: Path):
    """design.md §3.10's "plus external nodes referenced only by those edges"."""
    right = variant(
        large,
        tmp_path / "right.sqlite",
        drop_edges=[("python:pkg.mod.f0", LEAF, "calls")],
        drop_nodes=(LEAF,),
    )

    with pytest.warns(compare.VarianceWarning):
        result = compare.compare_indexes(large, right)

    assert result.verdict == compare.IN_VARIANCE_CLASS
    assert len(result.differences) == 2
    assert {difference.category for difference in result.variance} == {
        compare.CATEGORY_EDGES,
        compare.CATEGORY_NODES,
    }


def test_the_threshold_is_inclusive(tmp_path: Path, large: Path):
    """Exactly 0.01 % — 10 edges of 100,000 — is inside the class, as §3.10 writes it (≤)."""
    right = variant(large, tmp_path / "right.sqlite", drop_edges=chain_edges(10, start=100))

    with pytest.warns(compare.VarianceWarning):
        result = compare.compare_indexes(large, right)

    assert result.verdict == compare.IN_VARIANCE_CLASS
    assert result.variance_fraction == pytest.approx(compare.VARIANCE_THRESHOLD)


def test_call_edge_differences_above_the_threshold_are_a_defect(tmp_path: Path, large: Path):
    """One edge over the bound flips the verdict: the class is bounded, not open-ended."""
    right = variant(large, tmp_path / "right.sqlite", drop_edges=chain_edges(11, start=100))

    result = compare.compare_indexes(large, right)

    assert result.verdict == compare.DEFECT
    assert not result.ok
    assert result.variance_fraction > compare.VARIANCE_THRESHOLD
    # The differences are in-class by shape — the summary must say the threshold is why, and
    # must still name the rows even though none of them is a defect on its own.
    assert len(result.variance) == 11
    assert "threshold" in result.summary()
    assert "python:pkg.mod.f100" in result.summary()


def test_a_node_difference_beside_an_in_class_edge_difference_is_a_defect(
    tmp_path: Path, large: Path
):
    """ "solely": one internal-node difference disqualifies an otherwise in-class diff set."""
    right = variant(
        large,
        tmp_path / "right.sqlite",
        drop_edges=[*chain_edges(1, start=5), ("python:pkg.mod.f7", "python:pkg.mod.f8", "calls")],
        drop_nodes=("python:pkg.mod.f7",),
    )

    result = compare.compare_indexes(large, right)

    assert result.verdict == compare.DEFECT
    assert [difference.category for difference in result.defects] == [compare.CATEGORY_NODES]


# ---------------------------------------------------------------------------
# Reports (§5.3 documents, §5.4's `run` block)
# ---------------------------------------------------------------------------


def build_reports(directory: Path, *, run_id: str, analyzed: int = 2, extra: dict | None = None):
    """A full set of §5.3 reports, written by the product's own writers."""
    directory.mkdir(parents=True, exist_ok=True)
    run = reports.RunInfo.start(run_id).finish(1.5)
    counts = reports.coverage_counts(discovered=analyzed, analyzed=analyzed, skipped=0, excluded=0)
    documents = {
        reports.COVERAGE_REPORT: reports.coverage_document(
            run,
            counts,
            [reports.analyzed_row(f"pkg/mod{position}.py") for position in range(analyzed)],
        ),
        reports.EXCLUSIONS_REPORT: reports.exclusions_document(run, ()),
        reports.REANALYSIS_REPORT: reports.reanalysis_document(run, mode=reports.MODE_FULL),
        reports.DIAGNOSTICS_REPORT: reports.diagnostics_document(run, ()),
        reports.DEADCODE_REPORT: reports.deadcode_document(run, no_entry_points_warning=False),
        reports.CHANGE_WARNING_REPORT: reports.change_warning_document(run),
    }
    documents.update(extra or {})
    for filename, document in documents.items():
        reports.write_report(directory, filename, document)
    return directory


def test_reports_differing_only_in_the_run_block_are_equal(tmp_path: Path):
    """AC-44.2's strip: `run` is volatile, everything else in a report is not."""
    left = build_reports(tmp_path / "left", run_id="run-left")
    right = build_reports(tmp_path / "right", run_id="run-right")

    assert (
        json.loads((left / reports.COVERAGE_REPORT).read_text())["run"]
        != json.loads((right / reports.COVERAGE_REPORT).read_text())["run"]
    )
    assert compare.compare_reports(left, right).verdict == compare.EQUAL


def test_a_report_difference_outside_the_run_block_is_a_defect(tmp_path: Path):
    left = build_reports(tmp_path / "left", run_id="run-left", analyzed=2)
    right = build_reports(tmp_path / "right", run_id="run-right", analyzed=3)

    result = compare.compare_reports(left, right)

    assert result.verdict == compare.DEFECT
    keys = {difference.key for difference in result.differences}
    assert "coverage.json:counts.files_analyzed" in keys
    assert "coverage.json:files[2]" in keys


def test_a_missing_report_is_a_defect(tmp_path: Path):
    left = build_reports(tmp_path / "left", run_id="run-left")
    right = build_reports(tmp_path / "right", run_id="run-right")
    (right / reports.DEADCODE_REPORT).unlink()

    result = compare.compare_reports(left, right)

    assert result.verdict == compare.DEFECT
    assert result.differences[0].key == reports.DEADCODE_REPORT
    assert result.differences[0].present_only_in == "left"


def test_report_differences_are_never_in_the_variance_class(tmp_path: Path):
    """§5.4 defines one variance class, over call edges — a report is not in it."""
    left = build_reports(tmp_path / "left", run_id="run-left")
    right = build_reports(tmp_path / "right", run_id="run-right")
    document = json.loads((right / reports.DIAGNOSTICS_REPORT).read_text())
    document["diagnostics"] = [
        {
            "kind": "unresolved_call",
            "path": "pkg/mod0.py",
            "line": 3,
            "col": 4,
            "message": "unresolved",
            "extra": {},
        }
    ]
    (right / reports.DIAGNOSTICS_REPORT).write_text(json.dumps(document), encoding="utf-8")

    result = compare.compare_reports(left, right)

    assert result.verdict == compare.DEFECT
    assert result.variance == ()


# ---------------------------------------------------------------------------
# Whole runs, the assertion helper, and the command line
# ---------------------------------------------------------------------------


def build_out(directory: Path, *, run_id: str, nodes=SMALL_NODES, edges=SMALL_EDGES) -> Path:
    """An `--out` directory: `index.sqlite` plus `reports/` (design.md §5.1)."""
    directory.mkdir(parents=True, exist_ok=True)
    write_index(
        directory / INDEX_FILENAME,
        nodes,
        edges,
        files=SMALL_FILES,
        meta={"run_id": run_id, "created_at": f"2026-07-24T09:00:0{len(run_id) % 10}+00:00"},
    )
    build_reports(directory / reports.REPORTS_DIRNAME, run_id=run_id)
    return directory


def test_compare_runs_judges_index_and_reports_together(tmp_path: Path):
    left = build_out(tmp_path / "left", run_id="a")
    right = build_out(tmp_path / "right", run_id="bb")

    assert compare.compare_runs(left, right).verdict == compare.EQUAL

    # A difference in either half is a difference in the whole.
    (right / reports.REPORTS_DIRNAME / reports.DIAGNOSTICS_REPORT).unlink()
    assert compare.compare_runs(left, right).verdict == compare.DEFECT


def test_require_equivalent_passes_equality_and_raises_on_a_defect(tmp_path: Path, small: Path):
    right = variant(small, tmp_path / "right.sqlite", drop_nodes=(LONELY.id,))

    equal = compare.compare_indexes(small, small)
    assert compare.require_equivalent(equal) is equal

    with pytest.raises(AssertionError, match="DEFECT"):
        compare.require_equivalent(compare.compare_indexes(small, right))


def test_the_command_line_reports_the_verdict_and_exits(tmp_path: Path, capsys):
    left = build_out(tmp_path / "left", run_id="a")
    right = build_out(tmp_path / "right", run_id="bb")

    assert compare.main([str(left), str(right)]) == 0
    assert "equal" in capsys.readouterr().out

    (right / reports.REPORTS_DIRNAME / reports.DIAGNOSTICS_REPORT).unlink()
    assert compare.main([str(left), str(right), "--part", "reports"]) == 1
    assert "DEFECT" in capsys.readouterr().out
    # The index half is untouched, so restricting to it still passes.
    assert compare.main([str(left), str(right), "--part", "index"]) == 0
