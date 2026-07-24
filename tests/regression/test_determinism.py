"""The FR-44 determinism gate: two runs, one index (specs/tasks.md task 4.3).

design.md §3.10 (the comparator), §5.4 (the volatile-field register), D12 (canonical sort
at the write boundary, no hash-seed pinning); requirements FR-44 (AC-44.1, AC-44.2,
AC-44.3).

`test_compare.py` calibrates the instrument; this module points it at the real pipeline.
Every test here runs `analyze` for real — engine, detectors, reachability and all six
reports — over one fixture tree, and asserts the runs are equivalent under the comparator:

* AC-44.1/44.2 — the same tree analyzed twice produces equal indexes and equal reports;
* AC-44.3 — shuffling the order the candidate files are processed in changes nothing;
* D12 — none of it depends on the launcher: an unpinned (randomized) hash seed and
  `PYTHONHASHSEED=0` produce the same index, which is why nothing in this repository pins
  the seed to make the tests pass.

The fixture tree is deliberately not minimal. It carries a package with inheritance, a
lambda, a call into the standard library (an external leaf), a `getattr` dispatch (an
`unresolved_call` diagnostic), a declared console script and a `__main__` guard (two entry
points), a `.gitignore`d directory (an exclusion record), and a file that does not parse (a
skip) — so all six reports have content to disagree about, and every part of the index
does too.
"""

from __future__ import annotations

import io
import os
import random
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import compare
import pytest

from pastapathfinder import cli, reports, runner
from pastapathfinder.index import INDEX_FILENAME, open_index
from pastapathfinder.progress import ProgressSink

FIXTURE_TREE = {
    "pyproject.toml": (
        '[project]\nname = "determinism-fixture"\nversion = "0.1.0"\n\n'
        '[project.scripts]\ndemo = "pkg.app:main"\n'
    ),
    ".gitignore": "generated/\n",
    "generated/machine_written.py": "VALUE = 1\n",
    "pkg/__init__.py": "",
    "pkg/util.py": (
        '"""Leaf helpers."""\n\n'
        "import json\n\n\n"
        "def encode(payload):\n"
        "    return json.dumps(payload)\n\n\n"
        "def helper(name):\n"
        "    return name.upper()\n\n\n"
        "def unused():\n"
        '    return helper("never called")\n'
    ),
    "pkg/service.py": (
        "from pkg.util import encode, helper\n\n\n"
        "class Base:\n"
        "    def __init__(self, name):\n"
        "        self.name = name\n\n"
        "    def label(self):\n"
        "        return helper(self.name)\n\n\n"
        "class Service(Base):\n"
        "    def run(self, payload):\n"
        '        return encode({"label": self.label(), "payload": payload})\n\n'
        "    def dispatch(self, action, payload):\n"
        "        return getattr(self, action)(payload)\n"
    ),
    "pkg/app.py": (
        "from pkg.service import Service\n\n"
        "TRANSFORM = lambda value: value.strip()\n\n\n"
        "def main():\n"
        '    service = Service("demo")\n'
        '    return service.run(TRANSFORM("  data  "))\n\n\n'
        'if __name__ == "__main__":\n'
        "    main()\n"
    ),
    "pkg/broken.py": "def unparseable(:\n    return 1\n",
}


def analyze(root: Path, out: Path) -> runner.RunResult:
    """One real run with the streams captured — the same call the CLI makes.

    The progress sink keeps its default interval: the engine-build heartbeat is a thread
    that wakes on it, and driving it to zero spins the loop hot enough to dominate a run of
    this size.
    """
    return runner.run_analysis(
        root,
        out=out,
        progress=ProgressSink(io.StringIO()),
        stdout=io.StringIO(),
    )


@pytest.fixture
def fixture_root(tree) -> Path:
    return tree(FIXTURE_TREE, name="determinism")


def assert_substantial(out: Path) -> None:
    """Guard the gate itself: two *empty* indexes would compare equal too.

    Every assertion below is only worth something if the runs produced a real graph, so the
    content the comparator would be judging is checked to exist first.
    """
    content = compare.read_index(out / INDEX_FILENAME)
    kinds = {row["kind"] for row in content.nodes.values()}
    assert {"file", "module", "function", "class", "entry_point"} <= kinds
    assert content.call_edges > 5
    assert any(row["is_external"] for row in content.nodes.values())

    coverage = reports.load_report(out / reports.REPORTS_DIRNAME / reports.COVERAGE_REPORT)
    assert coverage["counts"]["files_analyzed"] >= 4
    assert coverage["counts"]["files_skipped"] == 1  # pkg/broken.py
    assert coverage["counts"]["entries_excluded"] == 1  # generated/, pruned as one entry
    diagnostics = reports.load_report(out / reports.REPORTS_DIRNAME / reports.DIAGNOSTICS_REPORT)
    assert diagnostics["diagnostics"], "the getattr dispatch should leave a diagnostic"


# ---------------------------------------------------------------------------
# AC-44.1 / AC-44.2 — two runs over an unchanged tree
# ---------------------------------------------------------------------------


def test_two_runs_over_an_unchanged_tree_are_equal(tmp_path: Path, fixture_root: Path):
    """AC-44.1 and AC-44.2 together, judged by the comparator over index *and* reports."""
    left, right = tmp_path / "out-a", tmp_path / "out-b"
    analyze(fixture_root, left)
    analyze(fixture_root, right)
    assert_substantial(left)

    result = compare.compare_runs(left, right)

    assert result.verdict == compare.EQUAL, result.summary()


def test_the_reports_are_equal_only_because_the_run_block_is_stripped(
    tmp_path: Path, fixture_root: Path
):
    """AC-44.2 with its premise proven: the two runs really did carry different `run` blocks."""
    left, right = tmp_path / "out-a", tmp_path / "out-b"
    first = analyze(fixture_root, left)
    second = analyze(fixture_root, right)

    assert first.run.run_id != second.run.run_id
    for filename in reports.REPORT_FILENAMES:
        left_document = reports.load_report(left / reports.REPORTS_DIRNAME / filename)
        right_document = reports.load_report(right / reports.REPORTS_DIRNAME / filename)
        assert left_document["run"]["run_id"] != right_document["run"]["run_id"], filename
        assert left_document != right_document, filename

    result = compare.compare_reports(
        left / reports.REPORTS_DIRNAME, right / reports.REPORTS_DIRNAME
    )

    assert result.verdict == compare.EQUAL, result.summary()


def test_the_index_volatiles_are_the_only_thing_the_two_runs_disagree_on(
    tmp_path: Path, fixture_root: Path
):
    """§5.4 against a real index: two builds differing in that pair and in nothing else."""
    left, right = tmp_path / "out-a", tmp_path / "out-b"
    analyze(fixture_root, left)
    analyze(fixture_root, right)

    with open_index(left / INDEX_FILENAME, read_only=True) as store:
        left_meta = store.meta()
    with open_index(right / INDEX_FILENAME, read_only=True) as store:
        right_meta = store.meta()

    assert left_meta["run_id"] != right_meta["run_id"]
    assert left_meta != right_meta
    # What the comparator sees: the same `meta` minus exactly `run_id` and `created_at`.
    stripped = {
        key: value for key, value in left_meta.items() if key not in ("run_id", "created_at")
    }
    assert compare.read_index(left / INDEX_FILENAME).meta == stripped
    assert compare.read_index(right / INDEX_FILENAME).meta == stripped


# ---------------------------------------------------------------------------
# AC-44.3 — processing order carries no information
# ---------------------------------------------------------------------------


def test_shuffled_candidate_order_produces_an_equal_index(
    tmp_path: Path, fixture_root: Path, monkeypatch
):
    """AC-44.3: the candidates reach the engine in a different order; nothing moves.

    `discovery` sorts its candidates, so the shuffle is injected around it — the same effect
    a parallel or differently-ordered enumeration would have (AC-44.3's own examples).
    """
    baseline_out, shuffled_out = tmp_path / "out-a", tmp_path / "out-b"
    analyze(fixture_root, baseline_out)

    real_discover = runner.discover
    orders: list[list[str]] = []

    def shuffling_discover(root, ruleset):
        result = real_discover(root, ruleset)
        candidates = list(result.candidates)
        random.Random(20260724).shuffle(candidates)
        orders.append([result.relpath(path) for path in candidates])
        return replace(result, candidates=candidates)

    monkeypatch.setattr(runner, "discover", shuffling_discover)
    analyze(fixture_root, shuffled_out)

    # The shuffle has to have done something, or the test proves nothing.
    assert orders and orders[0] != sorted(orders[0])

    result = compare.compare_runs(baseline_out, shuffled_out)

    assert result.verdict == compare.EQUAL, result.summary()


# ---------------------------------------------------------------------------
# D12 — determinism must not depend on the launcher
# ---------------------------------------------------------------------------


def run_cli(root: Path, out: Path, *, hash_seed: str | None) -> subprocess.CompletedProcess:
    """`python -m pastapathfinder analyze` in a fresh interpreter with a chosen hash seed."""
    environment = dict(os.environ)
    if hash_seed is None:
        environment.pop("PYTHONHASHSEED", None)
    else:
        environment["PYTHONHASHSEED"] = hash_seed
    return subprocess.run(
        [sys.executable, "-m", "pastapathfinder", "analyze", str(root), "--out", str(out)],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )


def test_the_index_does_not_depend_on_the_hash_seed(tmp_path: Path, fixture_root: Path):
    """D12: mypy is seed-independent (measured, `FINDINGS-mypy.md` Q4) and so are we.

    Nothing in this repository pins `PYTHONHASHSEED`; this test proves that is a fact about
    the pipeline rather than an accident of how the suite happens to be launched, by running
    the CLI once with randomization on and once with the seed pinned to 0.
    """
    randomized = subprocess.run(
        [sys.executable, "-c", "import sys; print(sys.flags.hash_randomization)"],
        capture_output=True,
        text=True,
        env={key: value for key, value in os.environ.items() if key != "PYTHONHASHSEED"},
        check=True,
    )
    assert randomized.stdout.strip() == "1", "the unpinned run must really be randomized"

    unpinned_out, pinned_out = tmp_path / "out-unpinned", tmp_path / "out-pinned"
    unpinned = run_cli(fixture_root, unpinned_out, hash_seed=None)
    pinned = run_cli(fixture_root, pinned_out, hash_seed="0")

    # AC-43.2: the fixture tree contains one unparseable file, so both runs exit 1.
    for completed in (unpinned, pinned):
        assert completed.returncode == cli.EXIT_PARTIAL, completed.stderr
    assert_substantial(unpinned_out)

    result = compare.compare_runs(unpinned_out, pinned_out)

    assert result.verdict == compare.EQUAL, result.summary()
