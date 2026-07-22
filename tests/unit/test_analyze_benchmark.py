"""`analyze` end to end over the pinned Django benchmark (specs/tasks.md task 2.5).

design.md §1 (data flow), §3.10 (`runner`), §8-O5 (the pins); requirements FR-6, FR-7
(AC-7.1), FR-12, FR-23, FR-29 (AC-29.1, AC-29.2), FR-43.

This is the first FR-29 datapoint from product code rather than from a prototype. The
prototype's references, for comparison (`FINDINGS-mypy.md` Q2, `FINDINGS-harness.md` §2):
engine build 10.5 s + bulk extraction 0.8 s + discovery/parse ~1.9 s ≈ **11.3 s**, 390 MB
peak RSS, 908 files, 0 parse failures. The bound is 600 s; an outcome inside the bound but
an order of magnitude off the reference is a signal worth reporting, not a pass to wave
through, so the measurements are printed whether or not the assertions hold.

The benchmark tree is not vendored — the regression suite that fetches it by hash arrives
with task 4.4 — so this module is opt-in, exactly like `test_extract_benchmark.py`:

    PASTAPATHFINDER_DJANGO_BENCHMARK=/path/to/django-checkout .venv/bin/python -m pytest \\
        tests/unit/test_analyze_benchmark.py -q -s
"""

import io
import resource
import sqlite3
import time

from test_extract_benchmark import DJANGO_FILE_COUNT, benchmark_package

from pastapathfinder import cli, reports, runner
from pastapathfinder.progress import ProgressSink

#: AC-29.1's bound: ten minutes on the reference machine of requirements §4.8.
FR29_SECONDS = 600.0

#: `FINDINGS-mypy.md` Q2's measurement of the same work in the prototype.
REFERENCE_SECONDS = 11.3


def graph_rows(index_path):
    """Every node and edge in the index, in canonical order."""
    connection = sqlite3.connect(index_path)
    try:
        return (
            list(connection.execute("SELECT * FROM nodes ORDER BY id")),
            list(connection.execute("SELECT * FROM edges ORDER BY src, dst, kind")),
        )
    finally:
        connection.close()


def analyze(root, out):
    """One timed run, with the streams captured. Returns `(result, seconds, stdout)`."""
    stdout = io.StringIO()
    started = time.perf_counter()
    result = runner.run_analysis(
        root,
        out=out,
        progress=ProgressSink(stream=io.StringIO()),
        stdout=stdout,
    )
    return result, time.perf_counter() - started, stdout.getvalue()


def test_full_analyze_of_the_pinned_django_package(tmp_path):
    """AC-29.1 and AC-7.1 over 908 real files, with the index and all six reports written."""
    package = benchmark_package()
    out = tmp_path / "out"

    result, seconds, stdout = analyze(package, out)
    peak_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024

    counts = reports.load_report(result.report_paths[reports.COVERAGE_REPORT])["counts"]
    nodes, edges = graph_rows(result.index_path)
    diagnostics = reports.load_report(result.report_paths[reports.DIAGNOSTICS_REPORT])[
        "diagnostics"
    ]
    kinds: dict[str, int] = {}
    for row in diagnostics:
        kinds[row["kind"]] = kinds.get(row["kind"], 0) + 1
    externals = sum(1 for node in nodes if node[7])
    calls = sum(1 for edge in edges if edge[2] == "calls")

    print(
        f"\nanalyze(django/): {seconds:.1f} s (bound {FR29_SECONDS:.0f} s, prototype "
        f"reference {REFERENCE_SECONDS} s), peak RSS {peak_mb:.0f} MB\n"
        f"  coverage: {counts}\n"
        f"  graph: {len(nodes)} nodes ({externals} external), {len(edges)} edges "
        f"({calls} calls)\n"
        f"  diagnostics: {sum(kinds.values())} ({kinds})\n"
        f"  exit code: {cli.exit_code_for(result)}"
    )

    # The run completed and published everything a run owes (FR-7, FR-42).
    assert result.completed
    assert result.index_path.is_file()
    for filename in reports.REPORT_FILENAMES:
        assert (result.reports_dir / filename).is_file(), filename

    # AC-7.1, from the structured counts alone (AC-42.2).
    assert (
        counts["entries_discovered"]
        == counts["files_analyzed"] + counts["files_skipped"] + counts["entries_excluded"]
    )
    # The pinned tree is 908 `.py` files and no exclusion rule matches inside `django/`.
    assert counts["files_analyzed"] + counts["files_skipped"] == DJANGO_FILE_COUNT

    # A populated graph, not an empty index that happened to be written.
    assert len(nodes) > 10_000
    assert calls > 10_000

    # AC-43.1/43.2: 0 or 1 depending on skips, never anything else on a completed run.
    assert cli.exit_code_for(result) in (cli.EXIT_SUCCESS, cli.EXIT_PARTIAL)
    assert (cli.exit_code_for(result) == cli.EXIT_PARTIAL) == (counts["files_skipped"] > 0)

    # AC-29.1. AC-29.2 is why this is asserted after the fact rather than as a timeout:
    # the run is never aborted to satisfy the bound.
    assert seconds <= FR29_SECONDS


def test_two_django_runs_produce_the_same_graph(tmp_path):
    """The gross-instability check task 2.5 asks for; the FR-44 gate proper is task 4.3.

    FR-44's 2026-07-18 amendment records the engine's variance class as *zero* at this
    scale (measured; the 3-edge locus is a pandas-scale observation), so two runs over an
    unchanged tree are compared row for row here.
    """
    package = benchmark_package()
    first, first_seconds, _ = analyze(package, tmp_path / "out")
    second, second_seconds, _ = analyze(package, tmp_path / "out")

    before_nodes, before_edges = graph_rows(first.index_path)
    after_nodes, after_edges = graph_rows(second.index_path)
    print(
        f"\ntwo runs: {first_seconds:.1f} s then {second_seconds:.1f} s; "
        f"nodes {len(before_nodes)} vs {len(after_nodes)}, "
        f"edges {len(before_edges)} vs {len(after_edges)}"
    )
    assert (len(after_nodes), len(after_edges)) == (len(before_nodes), len(before_edges))
    assert after_nodes == before_nodes
    assert after_edges == before_edges
