"""Post-run change detection (`postrun`, task 4.2).

design.md §3.10 (`postrun`), §5.3; requirements FR-38, EC-14.

The check is proven at two levels: directly against `postrun.snapshot`/`postrun.check`
(the mechanism — the pre-check gate and the hash-confirm authority), and end to end through
`runner.run_analysis` (the wiring — `change_warning.json` and the stdout warning).
"""

from __future__ import annotations

import hashlib
import io
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from pastapathfinder import postrun, reports, runner
from pastapathfinder.progress import ProgressSink
from stub_adapter import StubAdapter


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write(root: Path, relpath: str, text: str) -> None:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# The mechanism: snapshot + check
# ---------------------------------------------------------------------------


def test_snapshot_records_size_and_mtime_for_each_file(tmp_path):
    _write(tmp_path, "a.py", "x = 1\n")
    _write(tmp_path, "pkg/b.py", "y = 2\n")

    baseline = postrun.snapshot(tmp_path, ["a.py", "pkg/b.py"])

    assert set(baseline) == {"a.py", "pkg/b.py"}
    assert baseline["a.py"].size == len("x = 1\n")
    assert baseline["a.py"].mtime_ns == os.stat(tmp_path / "a.py").st_mtime_ns


def test_snapshot_skips_a_file_it_cannot_stat(tmp_path):
    """Best-effort: a missing file contributes no baseline entry, not an error."""
    baseline = postrun.snapshot(tmp_path, ["gone.py"])
    assert baseline == {}


def test_an_unchanged_run_reports_nothing(tmp_path):
    _write(tmp_path, "a.py", "x = 1\n")
    baseline = postrun.snapshot(tmp_path, ["a.py"])

    result = postrun.check(tmp_path, baseline, {"a.py": _hash("x = 1\n")})

    assert result.clean
    assert result.changed == []
    assert result.removed == []
    assert result.check_failures == []


def test_a_content_change_is_flagged(tmp_path):
    _write(tmp_path, "a.py", "x = 1\n")
    baseline = postrun.snapshot(tmp_path, ["a.py"])
    recorded = {"a.py": _hash("x = 1\n")}

    _write(tmp_path, "a.py", "x = 2  # edited during the run\n")
    result = postrun.check(tmp_path, baseline, recorded)

    assert result.changed == ["a.py"]
    assert result.removed == []
    assert result.check_failures == []


def test_an_mtime_flip_without_a_content_change_is_not_a_change(tmp_path):
    """AC-38.1: the difference is hash-confirmed, never mtime-inferred.

    The file's mtime moves but its bytes do not; the pre-check trips, the hash confirms
    identity, and no warning results.
    """
    _write(tmp_path, "a.py", "x = 1\n")
    baseline = postrun.snapshot(tmp_path, ["a.py"])
    recorded = {"a.py": _hash("x = 1\n")}

    # Move the mtime a full second forward without touching the content.
    later = os.stat(tmp_path / "a.py").st_mtime_ns + 1_000_000_000
    os.utime(tmp_path / "a.py", ns=(later, later))
    assert os.stat(tmp_path / "a.py").st_mtime_ns != baseline["a.py"].mtime_ns

    result = postrun.check(tmp_path, baseline, recorded)

    assert result.clean


def test_a_change_is_found_even_without_a_baseline_entry(tmp_path):
    """No pre-check baseline (snapshot failed for the file) falls back to hashing."""
    _write(tmp_path, "a.py", "x = 999\n")
    result = postrun.check(tmp_path, {}, {"a.py": _hash("x = 1\n")})
    assert result.changed == ["a.py"]


def test_a_removed_file_is_reported_removed(tmp_path):
    """AC-38.3: a file gone at check time is `removed`, not treated as unchanged."""
    _write(tmp_path, "a.py", "x = 1\n")
    baseline = postrun.snapshot(tmp_path, ["a.py"])
    recorded = {"a.py": _hash("x = 1\n")}

    (tmp_path / "a.py").unlink()
    result = postrun.check(tmp_path, baseline, recorded)

    assert result.removed == ["a.py"]
    assert result.changed == []
    assert result.check_failures == []


def test_an_unreadable_file_is_a_check_failure(tmp_path):
    """AC-38.3: a file that cannot be read during the check is named, never assumed current.

    The path is replaced by a directory — a state that stats cleanly (so it is neither
    `removed` nor a stat failure) but cannot be read as bytes. A baseline with a deliberately
    different size drives it past the pre-check to the hash step, where the read fails,
    independent of the test's uid and of how the host filesystem stats a directory.
    """
    _write(tmp_path, "a.py", "x = 1\n")
    recorded = {"a.py": _hash("x = 1\n")}
    baseline = {"a.py": postrun.FileState(size=999, mtime_ns=0)}

    (tmp_path / "a.py").unlink()
    (tmp_path / "a.py").mkdir()
    result = postrun.check(tmp_path, baseline, recorded)

    assert result.changed == []
    assert result.removed == []
    assert [entry["path"] for entry in result.check_failures] == ["a.py"]
    assert result.check_failures[0]["error"]


def test_results_are_sorted_and_stable(tmp_path):
    for name in ("c.py", "a.py", "b.py"):
        _write(tmp_path, name, "x = 1\n")
    recorded = {name: _hash("x = 1\n") for name in ("a.py", "b.py", "c.py")}
    baseline = postrun.snapshot(tmp_path, list(recorded))

    for name in ("c.py", "a.py", "b.py"):
        _write(tmp_path, name, f"x = 2  # {name}\n")

    result = postrun.check(tmp_path, baseline, recorded)
    assert result.changed == ["a.py", "b.py", "c.py"]


# ---------------------------------------------------------------------------
# The wiring: through `runner.run_analysis`
# ---------------------------------------------------------------------------


@dataclass
class _MutatingAdapter(StubAdapter):
    """A stub that edits, deletes, or merely re-touches a source file *after* recording it.

    The runner captures the FR-38 baseline before the adapter runs and records the content
    hash of the bytes the adapter read; mutating a file inside `analyze` therefore stages
    exactly the mid-run edit FR-38 exists to catch, with the recorded content preceding it.
    """

    edit: str = ""
    delete: str = ""
    touch: str = ""

    def analyze(self, root, files, cache_dir, changed, progress, prior_nodes=None):
        result = super().analyze(root, files, cache_dir, changed, progress, prior_nodes)
        if self.edit:
            (root / self.edit).write_text("def main():\n    return 42\n", encoding="utf-8")
        if self.delete:
            (root / self.delete).unlink()
        if self.touch:
            later = os.stat(root / self.touch).st_mtime_ns + 1_000_000_000
            os.utime(root / self.touch, ns=(later, later))
        return result


SIMPLE_TREE = {
    "pkg/__init__.py": "",
    "pkg/app.py": "def main():\n    return 1\n",
}


def _analyze(root: Path, out: Path, adapter):
    stdout = io.StringIO()
    result = runner.run_analysis(
        root,
        out=out,
        adapters=[adapter],
        progress=ProgressSink(io.StringIO(), interval=0.0),
        stdout=stdout,
    )
    warning = reports.load_report(result.report_paths[reports.CHANGE_WARNING_REPORT])
    return warning, stdout.getvalue()


def test_a_clean_run_writes_empty_lists_and_no_warning_line(tree, out_dir):
    """AC-38.2: nothing changed → empty lists, and no warning printed."""
    root = tree(SIMPLE_TREE)
    warning, stdout = _analyze(root, out_dir, StubAdapter())

    assert warning["changed"] == []
    assert warning["removed"] == []
    assert warning["check_failures"] == []
    assert "changed while this run was in progress" not in stdout


def test_a_file_edited_during_the_run_is_named_and_warned(tree, out_dir):
    """AC-38.1: a file mutated between read and completion is named, with a re-analyze note."""
    root = tree(SIMPLE_TREE)
    warning, stdout = _analyze(root, out_dir, _MutatingAdapter(edit="pkg/app.py"))

    assert warning["changed"] == ["pkg/app.py"]
    assert warning["note"] == reports.CHANGE_WARNING_NOTE
    assert "re-analyze" in stdout
    assert "changed: pkg/app.py" in stdout
    # AC-38.1 / EC-14: the warning is never rendered as a guarantee of freshness.
    assert reports.CHANGE_WARNING_NOTE in stdout


def test_a_file_removed_during_the_run_is_listed_removed(tree, out_dir):
    """AC-38.3, wired: a file deleted after being read is `removed` in the report."""
    root = tree(SIMPLE_TREE)
    warning, stdout = _analyze(root, out_dir, _MutatingAdapter(delete="pkg/app.py"))

    assert warning["removed"] == ["pkg/app.py"]
    assert warning["changed"] == []
    assert "removed: pkg/app.py" in stdout


def test_a_merely_retouched_file_does_not_warn(tree, out_dir):
    """AC-38.1, wired: mtime moved but bytes did not — the run stays clean."""
    root = tree(SIMPLE_TREE)
    warning, stdout = _analyze(root, out_dir, _MutatingAdapter(touch="pkg/app.py"))

    assert warning["changed"] == []
    assert warning["removed"] == []
    assert "changed while this run was in progress" not in stdout


@pytest.mark.parametrize("full", [False, True])
def test_the_check_runs_on_every_run_path(tree, out_dir, full):
    """FR-38 fires at the completion of *every* run — including the incremental and --full
    re-run paths, not only the first full write."""
    root = tree(SIMPLE_TREE)
    _analyze(root, out_dir, StubAdapter())  # first run writes the index

    result = runner.run_analysis(
        root,
        out=out_dir,
        adapters=[StubAdapter()],
        progress=ProgressSink(io.StringIO(), interval=0.0),
        stdout=io.StringIO(),
        full=full,
    )
    doc = reports.load_report(result.report_paths[reports.CHANGE_WARNING_REPORT])
    assert doc["changed"] == []
    assert doc["note"] == reports.CHANGE_WARNING_NOTE
