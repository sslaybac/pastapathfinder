"""The benchmark regression suite: FR-29, FR-30, AC-29.3 and FR-44 at scale (task 4.4).

design.md §8-O5 (the pins), D1, D1a (this suite *is* the revalidation suite that policy
names), §3.5; requirements FR-29 (AC-29.1, AC-29.2, AC-29.3), FR-30 (AC-30.1), FR-44,
§4.8 (the reference machine and the two benchmark designations).

Everything here is **slow and opt-in**: `pyproject.toml` excludes `-m slow` from the default
run, so `pytest` stays a seconds-scale developer loop and these run when asked:

    python tests/regression/benchmarks.py        # fetch the pinned checkouts once
    pytest tests/regression/test_benchmarks.py -m slow -s

Without a checkout at the pinned commit every test here skips with the command that
produces one — never silently passes.

Three things separate this module from the ad-hoc benchmark tests under `tests/unit/`
(tasks 2.2 and 2.5, which measured the extractor and the first end-to-end run):

* the tree is **pinned and verified** by commit before anything is timed (`benchmarks.py`);
* the measurement is a **subprocess `pastapathfinder analyze`**, because AC-29.1 bounds
  "wall-clock time from invocation to all artifacts written" — interpreter start-up and
  report writing included, which is what the user actually waits for;
* every measurement is **printed** next to the prototype reference it should resemble.
  AC-29.2 is why the bounds are asserted after the fact rather than imposed as timeouts:
  the run is never aborted to satisfy a bound, and an outcome inside the bound but far
  from the reference is a signal worth reading, not a pass to wave through.
"""

from __future__ import annotations

import re
import resource
import shutil
import subprocess
import sys
import time
import warnings
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path

import benchmarks
import compare
import pytest

from pastapathfinder import reports
from pastapathfinder.index import INDEX_FILENAME

pytestmark = pytest.mark.slow

#: AC-29.1's bound: ten minutes on the reference machine of requirements §4.8.
FR29_SECONDS = 600.0

#: AC-30.1's bound: thirty seconds after ≤ 5 changed files.
FR30_SECONDS = 30.0

#: `FINDINGS-mypy.md` Q2: build 10.5 s + extraction 0.8 s + discovery/parse ~1.9 s.
DJANGO_REFERENCE_SECONDS = 11.3

#: `FINDINGS-mypy.md` Q3 / `FINDINGS-session5.md` Part 1: 13.2 s for this change set, plus
#: D18's ~2.0 s full detector pass over the 908 files, so ~15 s against the 30 s bound.
DJANGO_INCREMENTAL_REFERENCE_SECONDS = 15.2

#: `FINDINGS-session5.md` Part 2: 53.3 s, 1,267 MB peak, 0 file casualties.
PANDAS_REFERENCE_SECONDS = 53.3

#: The 5-file change set the whole engine evaluation used, so the FR-30 number here is
#: comparable to the ones that selected the engine (`FINDINGS-jedi.md` Q3: the pinned five;
#: `core.exceptions` alone has 162 direct importers, `utils.functional` 118).
DJANGO_CHANGE_SET = (
    "utils/translation/__init__.py",
    "db/models/query.py",
    "core/exceptions.py",
    "utils/functional.py",
    "db/models/fields/__init__.py",
)

#: pandas ships `.pyi` stubs for its compiled `_libs`; calls into them must resolve to
#: external leaf nodes rather than collapsing to `Any`. `FINDINGS-session5.md` Part 2
#: measured ~3,747 such edges — an order-of-magnitude drop is the regression this guards.
PANDAS_LIBS_PREFIX = "python:pandas._libs."
PANDAS_LIBS_MINIMUM_EDGES = 1_000


# ---------------------------------------------------------------------------
# Running the product the way a user does
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Run:
    """One completed `analyze` invocation and what it cost."""

    out: Path
    seconds: float
    peak_mb: float
    completed: subprocess.CompletedProcess[str]

    @property
    def reports_dir(self) -> Path:
        return self.out / reports.REPORTS_DIRNAME

    def report(self, filename: str) -> dict:
        return reports.load_report(self.reports_dir / filename)

    @property
    def counts(self) -> dict[str, int]:
        return self.report(reports.COVERAGE_REPORT)["counts"]

    def index(self) -> compare.IndexContent:
        return compare.read_index(self.out / INDEX_FILENAME)


def analyze(root: Path, out: Path) -> Run:
    """`pastapathfinder analyze <root> --out <out>` in a fresh interpreter, timed.

    A subprocess, not an in-process call, because AC-29.1 bounds "wall-clock time from
    invocation to all artifacts written" — interpreter start-up and report writing included.

    Peak RSS is the high-water mark across this session's child processes — these runs
    dwarf anything else the suite spawns, so it reads as this run's peak, and it is printed
    for comparison rather than asserted (no requirement bounds memory).
    """
    started = time.perf_counter()
    completed = subprocess.run(
        [sys.executable, "-m", "pastapathfinder", "analyze", str(root), "--out", str(out)],
        capture_output=True,
        text=True,
        check=False,
    )
    seconds = time.perf_counter() - started
    peak_mb = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024
    return Run(out=out, seconds=seconds, peak_mb=peak_mb, completed=completed)


def assert_completed(run: Run) -> None:
    """A finished run: exit 0 or 1 (never 2), the index written, all six reports present."""
    assert run.completed.returncode in (0, 1), (
        f"exit {run.completed.returncode}\n{run.completed.stdout[-4000:]}\n"
        f"{run.completed.stderr[-4000:]}"
    )
    assert (run.out / INDEX_FILENAME).is_file()
    for filename in reports.REPORT_FILENAMES:
        assert (run.reports_dir / filename).is_file(), filename
    counts = run.counts
    # AC-7.1's reconciliation, from the structured counts alone (AC-42.2).
    assert (
        counts[reports.COUNT_DISCOVERED]
        == counts[reports.COUNT_ANALYZED]
        + counts[reports.COUNT_SKIPPED]
        + counts[reports.COUNT_EXCLUDED]
    )


def diagnostic_kinds(run: Run) -> dict[str, int]:
    kinds: dict[str, int] = {}
    for entry in run.report(reports.DIAGNOSTICS_REPORT)["diagnostics"]:
        kinds[entry["kind"]] = kinds.get(entry["kind"], 0) + 1
    return kinds


def describe(label: str, run: Run, content: compare.IndexContent, bound: str) -> str:
    calls = content.call_edges
    externals = sum(1 for row in content.nodes.values() if row["is_external"])
    return (
        f"\n{label}: {run.seconds:.1f} s ({bound}), peak RSS {run.peak_mb:.0f} MB\n"
        f"  coverage: {run.counts}\n"
        f"  graph: {len(content.nodes)} nodes ({externals} external), "
        f"{len(content.edges)} edges ({calls} calls)\n"
        f"  diagnostics: {diagnostic_kinds(run)}\n"
        f"  exit code: {run.completed.returncode}"
    )


# ---------------------------------------------------------------------------
# Fixtures: the pinned trees, and the runs that are expensive enough to share
# ---------------------------------------------------------------------------


def require(benchmark: benchmarks.Benchmark) -> Path:
    try:
        return benchmarks.verify(benchmark)
    except benchmarks.BenchmarkUnavailable as unavailable:
        pytest.skip(str(unavailable))


@pytest.fixture(scope="session")
def django_package() -> Path:
    """The pinned `django/` package — the FR-29/FR-30 performance reference."""
    return require(benchmarks.DJANGO)


@pytest.fixture(scope="session")
def pandas_package() -> Path:
    """The pinned `pandas/` package — the AC-29.3 dynamism and robustness reference."""
    return require(benchmarks.PANDAS)


@pytest.fixture(scope="session")
def pandas_runs(pandas_package: Path, tmp_path_factory) -> tuple[Run, Run]:
    """Two full pandas analyses of the same tree, into two output directories.

    Shared because they are the two most expensive things in the suite and the same pair
    answers both questions asked of them: the first run carries AC-29.3, and the pair
    carries FR-44 determinism at scale.
    """
    base = tmp_path_factory.mktemp("pandas")
    first = analyze(pandas_package, base / "out-a")
    second = analyze(pandas_package, base / "out-b")
    return first, second


# ---------------------------------------------------------------------------
# Django core — FR-29
# ---------------------------------------------------------------------------


def test_django_full_analysis_is_within_the_fr29_bound(django_package: Path, tmp_path: Path):
    """AC-29.1: invocation to all artifacts written, ≤ 600 s on the reference machine."""
    run = analyze(django_package, tmp_path / "out")
    content = run.index()
    print(
        describe(
            "django full analyze",
            run,
            content,
            f"bound {FR29_SECONDS:.0f} s, prototype reference {DJANGO_REFERENCE_SECONDS} s",
        )
    )

    assert_completed(run)
    counts = run.counts
    # The pin's own census: 908 `.py` files, and no exclusion rule matches inside `django/`.
    assert counts[reports.COUNT_ANALYZED] + counts[reports.COUNT_SKIPPED] == (
        benchmarks.DJANGO.file_count
    )
    # `FINDINGS-mypy.md` Q2: a complete graph, zero parse failures.
    assert counts[reports.COUNT_SKIPPED] == 0
    assert content.call_edges > 10_000

    # AC-29.1, asserted after the fact — AC-29.2: the run is never aborted for the bound.
    assert run.seconds <= FR29_SECONDS


# ---------------------------------------------------------------------------
# Django core — FR-30
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def django_worktree(django_package: Path, tmp_path_factory) -> Path:
    """A writable copy of the pinned package: FR-30 needs to edit files, the pin must not.

    The copy keeps the package's own name, so the adapter's build root (the *parent* of the
    analyzed package — `FINDINGS-mypy.md` §2 trap 2) is the same shape it is for the pin.
    """
    destination = tmp_path_factory.mktemp("django-worktree") / benchmarks.DJANGO.package
    shutil.copytree(django_package, destination)
    return destination


def test_django_reanalysis_after_five_changed_files_is_within_the_fr30_bound(
    django_worktree: Path, tmp_path_factory
):
    """AC-30.1: five real content changes, re-analyzed incrementally in ≤ 30 s.

    The change set and the protocol are the engine evaluation's, so this number sits beside
    the ones that chose mypy: touch the five high-fan-in core modules (279 files by direct
    importers alone), then time a fresh process against the warm index and mypy cache.
    """
    out = tmp_path_factory.mktemp("django-incremental") / "out"
    baseline = analyze(django_worktree, out)
    assert_completed(baseline)
    print(f"\ndjango baseline (for the incremental measurement): {baseline.seconds:.1f} s")

    for relpath in DJANGO_CHANGE_SET:
        path = django_worktree / relpath
        assert path.is_file(), relpath
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n# pastapathfinder FR-30 benchmark edit\n_FR30_BENCHMARK_EDIT = True\n")

    run = analyze(django_worktree, out)
    content = run.index()
    print(
        describe(
            "django re-analyze after 5 changed files",
            run,
            content,
            f"bound {FR30_SECONDS:.0f} s, prototype reference "
            f"{DJANGO_INCREMENTAL_REFERENCE_SECONDS} s",
        )
    )

    assert_completed(run)
    reanalysis = run.report(reports.REANALYSIS_REPORT)
    assert reanalysis["mode"] == reports.MODE_INCREMENTAL, reanalysis["mode"]
    changed = {
        entry["path"]
        for entry in reanalysis["reprocessed"]
        if entry["reason"] == reports.REASON_CONTENT_CHANGED
    }
    assert changed == set(DJANGO_CHANGE_SET), sorted(changed)
    # FR-35: the rest of the re-processed set is dependents, not a silent full rebuild.
    assert all(
        entry["reason"] in (reports.REASON_CONTENT_CHANGED, reports.REASON_DEPENDENT)
        for entry in reanalysis["reprocessed"]
    )

    # AC-30.1.
    assert run.seconds <= FR30_SECONDS


# ---------------------------------------------------------------------------
# pandas — AC-29.3
# ---------------------------------------------------------------------------


def test_pandas_runs_to_completion(pandas_runs: tuple[Run, Run]):
    """AC-29.3: 664 k lines run to completion with every artifact — and no time bound.

    The 10-minute bound is deliberately **not** asserted here (requirements §4.8 designates
    pandas the dynamism benchmark, not the performance one); the duration is printed so a
    regression is still visible.
    """
    run, _ = pandas_runs
    content = run.index()
    print(describe("pandas full analyze", run, content, f"reference {PANDAS_REFERENCE_SECONDS} s"))

    assert_completed(run)
    counts = run.counts
    assert counts[reports.COUNT_ANALYZED] + counts[reports.COUNT_SKIPPED] == (
        benchmarks.PANDAS.file_count
    )
    # `FINDINGS-session5.md` Part 2: zero parse failures, zero file casualties.
    assert counts[reports.COUNT_SKIPPED] == 0
    assert counts[reports.COUNT_ANALYZED] == benchmarks.PANDAS.file_count


def test_pandas_libs_calls_resolve_through_the_shipped_stubs(pandas_runs: tuple[Run, Run]):
    """Missing compiled `.so` files must not abort the build, nor collapse `_libs` to `Any`.

    pandas ships `.pyi` stubs for `pandas._libs`, whose extension modules are absent from
    this environment. The correct outcome is a population of external leaf nodes named
    `pandas._libs.*` with call edges into them (FR-36); an `Any`-collapse — which would look
    like a clean run with those edges simply gone — is a regression, not expected behavior.
    """
    run, _ = pandas_runs
    content = run.index()
    libs_edges = [
        key
        for key in content.edges
        if key[2] == compare.CALLS and key[1].startswith(PANDAS_LIBS_PREFIX)
    ]
    libs_nodes = {key[1] for key in libs_edges}
    print(
        f"\npandas._libs: {len(libs_edges)} call edges into {len(libs_nodes)} nodes "
        f"(reference ~3,747 edges, `FINDINGS-session5.md` Part 2)"
    )

    assert len(libs_edges) >= PANDAS_LIBS_MINIMUM_EDGES
    # AC-36.1/37.2: they are external leaves — unanalyzed, spanless, and terminal.
    for node_id in libs_nodes:
        row = content.nodes[node_id]
        assert row["is_external"], node_id
        assert row["file_path"] is None, node_id
    assert not [key for key in content.edges if key[0] in libs_nodes]


# ---------------------------------------------------------------------------
# FR-44 — determinism at scale
# ---------------------------------------------------------------------------


def test_two_pandas_runs_are_deterministic(pandas_runs: tuple[Run, Run]):
    """FR-44 at the scale where the engine's variance class was measured.

    Expected: `equal`, or `in_variance_class` — the 3-of-88,228-edge locus
    (`FINDINGS-session5.md` Part 2) that FR-44's 2026-07-18 amendment documents. Anything
    else fails. An in-class outcome is *reported*, never quietly accepted: `compare` warns,
    and the warning is captured and printed here.
    """
    first, second = pandas_runs
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always", compare.VarianceWarning)
        result = compare.compare_runs(first.out, second.out)
    for warning in captured:
        print(f"\nFR-44 variance reported: {warning.message}")

    print(f"\npandas determinism: {result.summary()}")
    compare.require_equivalent(result)
    assert result.verdict in (compare.EQUAL, compare.IN_VARIANCE_CLASS)
    assert (result.verdict == compare.IN_VARIANCE_CLASS) == bool(captured)


# ---------------------------------------------------------------------------
# The pins themselves
# ---------------------------------------------------------------------------


def test_the_readme_publishes_the_pins_verbatim():
    """D1a's checklist is only usable if the README's hashes are the ones the code uses.

    Cheap enough to run unconditionally were it not for the module-wide `slow` mark; it
    needs no checkout, only the two files.
    """
    readme = (Path(__file__).parent / "README.md").read_text(encoding="utf-8")
    for benchmark in benchmarks.BENCHMARKS:
        assert benchmark.commit in readme, benchmark.name
        # `specs/tasks.md` writes the repositories scheme-less, and §1 copies it verbatim.
        assert benchmark.url.removeprefix("https://") in readme, benchmark.name
        assert f"{benchmark.file_count:,}" in readme, benchmark.name
        assert f"{benchmark.code_lines:,}" in readme, benchmark.name


def test_the_pinned_checkouts_are_at_the_pinned_commits(django_package: Path, pandas_package: Path):
    """Every number above is a statement about these two trees; here is the proof of which.

    `benchmarks.verify()` has already refused a mismatch by the time the fixtures resolve —
    this states the fact in the record, and prints the census the pins carry.
    """
    packages = {benchmarks.DJANGO: django_package, benchmarks.PANDAS: pandas_package}
    for benchmark, package in packages.items():
        location = benchmarks.checkout_path(benchmark)
        assert benchmarks.head_commit(location) == benchmark.commit
        print(
            f"\n{benchmark.name}: {package} @ {benchmark.commit} "
            f"({benchmark.file_count:,} files, {benchmark.code_lines:,} lines; "
            f"pin source: {benchmark.source})"
        )


def test_the_engine_pin_is_exact_and_installed(tmp_path: Path):
    """D1/D1a: the pin is exact, the installed engine is that version, and so is the index's.

    The assertion an upgrade trips first — bump the pin without running the D1a sequence and
    this says so in one line. It analyzes a two-file tree rather than a benchmark, because
    what it checks is the pin, not the codebase.
    """
    pyproject = (Path(__file__).parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    pinned = re.search(r'"mypy==([^"]+)"', pyproject)
    assert pinned, "pyproject.toml must pin mypy exactly (design.md D1a)"
    assert version("mypy") == pinned.group(1)

    root = tmp_path / "tiny"
    root.mkdir()
    (root / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    run = analyze(root, tmp_path / "out")
    assert_completed(run)

    meta = run.index().meta
    assert meta["engine"] == "mypy"
    assert meta["engine_version"] == pinned.group(1)
