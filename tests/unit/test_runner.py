"""`analyze`, end to end, with the stub adapter standing in for the engine.

design.md §1 (the run order), §3.1 (exit codes), §3.4, §3.10, §5.1, §5.3, D9, D17;
requirements FR-5, FR-7, FR-23, FR-34, FR-41, FR-42, FR-43, EC-10.

Milestone 1's walking skeleton: a real tree is walked, real exclusion rules are applied,
a real index is written and the six real reports are produced — only the graph extraction
is a double (`tests/stub_adapter.py`).
"""

from __future__ import annotations

import io
import json
import os
import stat
from pathlib import Path

import pytest

from pastapathfinder import cli, reports, runner
from pastapathfinder.adapters.base import LanguageAdapter, SourceFile
from pastapathfinder.index import INDEX_FILENAME, open_index
from pastapathfinder.progress import ProgressSink
from pastapathfinder.schema import SCHEMA_VERSION, Diag
from stub_adapter import StubAdapter

SIMPLE_TREE = {
    "pkg/__init__.py": "",
    "pkg/app.py": "def main():\n    return 1\n",
    "README.md": "not a source file\n",
}


def analyze(root: Path, out: Path, adapter=None, **kwargs):
    """Run one analysis with an injected adapter and captured output streams."""
    stdout = kwargs.pop("stdout", io.StringIO())
    stderr = kwargs.pop("stderr", io.StringIO())
    result = runner.run_analysis(
        root,
        out=out,
        adapters=[adapter if adapter is not None else StubAdapter()],
        progress=ProgressSink(stderr, interval=0.0),
        stdout=stdout,
        **kwargs,
    )
    return result, stdout.getvalue(), stderr.getvalue()


def report(result: runner.RunResult, filename: str) -> dict:
    return reports.load_report(result.report_paths[filename])


# ---------------------------------------------------------------------------
# The whole path
# ---------------------------------------------------------------------------


def test_a_clean_run_writes_the_index_and_all_six_reports(tree, out_dir):
    root = tree(SIMPLE_TREE)
    result, stdout, _ = analyze(root, out_dir)

    assert result.completed
    assert result.index_path == out_dir / INDEX_FILENAME
    assert result.index_path.is_file()
    with open_index(result.index_path) as index:
        assert index.get_meta("schema_version") == str(SCHEMA_VERSION)
        assert index.get_meta("root_path") == str(root)
        assert index.get_meta("engine") == "stub"
        assert set(index.content_hashes()) == {"pkg/__init__.py", "pkg/app.py"}

    for filename in reports.REPORT_FILENAMES:
        path = result.reports_dir / filename
        assert path.is_file(), filename
        # AC-42.1: every report parses, and carries the format version.
        assert json.loads(path.read_text(encoding="utf-8"))["format_version"] == 1
    assert "Index:" in stdout


def test_the_stub_adapter_satisfies_the_protocol():
    """FR-23: the seam is a protocol, and the double implements it rather than mimicking it."""
    assert isinstance(StubAdapter(), LanguageAdapter)


def test_the_adapter_receives_absolute_paths_and_root_relative_names(tree, out_dir):
    root = tree(SIMPLE_TREE)
    adapter = StubAdapter()
    analyze(root, out_dir, adapter)
    assert [source.relpath for source in adapter.seen] == ["pkg/__init__.py", "pkg/app.py"]
    assert all(
        isinstance(source, SourceFile) and source.path.is_absolute() for source in adapter.seen
    )


def test_non_source_files_are_not_analysis_inputs(tree, out_dir):
    """AC-1.2 through the run: README.md never reaches the adapter or the index."""
    root = tree(SIMPLE_TREE)
    result, _, _ = analyze(root, out_dir)
    paths = {row["path"] for row in report(result, reports.COVERAGE_REPORT)["files"]}
    assert "README.md" not in paths


def test_an_empty_root_completes_with_zero_candidates(tree, out_dir):
    """EC-10: a root with no recognized sources completes and says so."""
    root = tree({"notes.txt": "nothing here\n"})
    result, stdout, _ = analyze(root, out_dir)
    counts = report(result, reports.COVERAGE_REPORT)["counts"]
    assert counts == {
        "entries_discovered": 0,
        "files_analyzed": 0,
        "files_skipped": 0,
        "entries_excluded": 0,
    }
    assert "No recognized Python sources were discovered." in stdout


# ---------------------------------------------------------------------------
# Coverage accounting (FR-7, AC-7.1/7.2, D17)
# ---------------------------------------------------------------------------


def test_counts_reconcile_and_units_follow_d17(tree, out_dir):
    """AC-7.1/42.2: a pruned directory is one excluded entry; matched files are one each."""
    root = tree(
        {
            "pkg/app.py": "x = 1\n",
            ".gitignore": "ignored_a.py\nignored_b.py\n",
            "ignored_a.py": "x = 1\n",
            "ignored_b.py": "x = 1\n",
            "venv/lib/one.py": "x = 1\n",
            "venv/lib/two.py": "x = 1\n",
            "venv/lib/three.py": "x = 1\n",
        }
    )
    result, _, _ = analyze(root, out_dir)
    document = report(result, reports.COVERAGE_REPORT)
    counts = document["counts"]

    assert (
        counts["entries_discovered"]
        == counts["files_analyzed"] + counts["files_skipped"] + counts["entries_excluded"]
    )
    assert counts["files_analyzed"] == 1
    assert counts["entries_excluded"] == 3  # venv/ once, plus the two matched files

    rows = {row["path"]: row for row in document["files"] if row["status"] == "excluded"}
    assert rows["venv"]["is_dir"] is True
    assert rows["venv"]["rule"] == {"pattern": "venv/", "source": "default:python"}
    assert rows["ignored_a.py"]["is_dir"] is False
    assert rows["ignored_a.py"]["rule"]["source"] == "gitignore:.gitignore"
    # The pruned directory's three files are neither enumerated nor counted (§8-O1).
    assert not any(path.startswith("venv/") for path in rows)


def test_a_pruned_directory_of_500_files_costs_one_entry(tree, out_dir):
    """The D17 counting unit at scale: one record, nothing beneath it enumerated."""
    files = {"pkg/app.py": "x = 1\n"}
    files.update({f"venv/lib/mod{n}.py": "x = 1\n" for n in range(500)})
    root = tree(files)
    result, _, _ = analyze(root, out_dir)
    document = report(result, reports.COVERAGE_REPORT)
    assert document["counts"]["entries_excluded"] == 1
    assert document["counts"]["entries_discovered"] == 2


def test_an_inconsistent_adapter_fails_the_run_loudly(tree, out_dir):
    """AC-7.1: an injected mismatch is a pipeline defect and stops the run."""
    root = tree(SIMPLE_TREE)
    adapter = StubAdapter(phantom_files=["pkg/never_discovered.py"])
    with pytest.raises(reports.CoverageMismatchError) as raised:
        analyze(root, out_dir, adapter)
    assert "AC-7.1" in str(raised.value)
    assert not (out_dir / reports.REPORTS_DIRNAME / reports.COVERAGE_REPORT).exists()
    # Nor is an index published claiming a coverage the run could not account for.
    assert not (out_dir / INDEX_FILENAME).exists()


def test_a_skipped_file_carries_a_human_readable_reason(tree, out_dir):
    """AC-7.2: the skip entry says why, in words."""
    root = tree(SIMPLE_TREE)
    adapter = StubAdapter(skip={"pkg/app.py": "line 3: unexpected indent"})
    result, stdout, _ = analyze(root, out_dir, adapter)
    rows = {row["path"]: row for row in report(result, reports.COVERAGE_REPORT)["files"]}
    assert rows["pkg/app.py"]["status"] == "skipped"
    assert rows["pkg/app.py"]["reason"] == "line 3: unexpected indent"
    assert "line 3: unexpected indent" in stdout


# ---------------------------------------------------------------------------
# Exclusion reporting (FR-5)
# ---------------------------------------------------------------------------


def test_exclusions_appear_with_their_rule(tree, out_dir):
    """AC-5.1: every excluded path with the rule that excluded it."""
    root = tree({"pkg/app.py": "x = 1\n", "build/gen.py": "x = 1\n"})
    result, _, _ = analyze(root, out_dir)
    document = report(result, reports.EXCLUSIONS_REPORT)
    assert document["none_excluded"] is False
    assert document["exclusions"] == [
        {"path": "build", "is_dir": True, "pattern": "build/", "source": "default:python"}
    ]


def test_first_run_points_at_the_exclusion_report(tree, out_dir):
    """AC-5.2: the first analysis of a codebase puts the report in front of the user."""
    root = tree({"pkg/app.py": "x = 1\n", "build/gen.py": "x = 1\n"})
    result, first_stdout, _ = analyze(root, out_dir)
    assert "First run of this codebase" in first_stdout
    assert str(result.report_paths[reports.EXCLUSIONS_REPORT]) in first_stdout

    _, second_stdout, _ = analyze(root, out_dir)
    assert "First run of this codebase" not in second_stdout


def test_an_exclusion_free_run_still_writes_the_report(tree, out_dir):
    """AC-5.3: nothing excluded is stated, not omitted."""
    root = tree({"pkg/app.py": "x = 1\n"})
    result, stdout, _ = analyze(root, out_dir)
    document = report(result, reports.EXCLUSIONS_REPORT)
    assert document["exclusions"] == []
    assert document["none_excluded"] is True
    assert "Exclusions: none" in stdout


def test_user_configuration_is_applied(tree, out_dir):
    """FR-4 through the run: `[exclude] add` reaches the rule set and the report."""
    root = tree(
        {
            "pkg/app.py": "x = 1\n",
            "pkg/gen_pb2.py": "x = 1\n",
            ".pastapathfinder.toml": '[exclude]\nadd = ["*_pb2.py"]\n',
        }
    )
    result, _, _ = analyze(root, out_dir)
    document = report(result, reports.EXCLUSIONS_REPORT)
    assert document["exclusions"] == [
        {"path": "pkg/gen_pb2.py", "is_dir": False, "pattern": "*_pb2.py", "source": "user:exclude"}
    ]


# ---------------------------------------------------------------------------
# The reports later tasks fill (the C-10 convention)
# ---------------------------------------------------------------------------


def test_the_remaining_reports_are_produced_empty_on_a_clean_run(tree, out_dir):
    root = tree(SIMPLE_TREE)
    result, _, _ = analyze(root, out_dir)

    diagnostics = report(result, reports.DIAGNOSTICS_REPORT)
    assert diagnostics["diagnostics"] == []

    reanalysis = report(result, reports.REANALYSIS_REPORT)
    assert reanalysis["mode"] == reports.MODE_FULL
    assert reanalysis["reprocessed"] == [] and reanalysis["removed"] == []

    warning = report(result, reports.CHANGE_WARNING_REPORT)
    assert warning["changed"] == warning["removed"] == warning["check_failures"] == []
    assert warning["note"] == reports.CHANGE_WARNING_NOTE

    deadcode = report(result, reports.DEADCODE_REPORT)
    assert deadcode["unreachable"] == []
    assert deadcode["no_entry_points_warning"] is True


def test_detected_entry_points_clear_the_no_entry_points_warning(tree, out_dir):
    root = tree(SIMPLE_TREE)
    result, _, _ = analyze(root, out_dir, StubAdapter(entry_points=True))
    assert report(result, reports.DEADCODE_REPORT)["no_entry_points_warning"] is False


def test_diagnostics_from_every_stage_reach_the_report(tree, out_dir):
    """Rule-set, discovery and adapter anomalies land in one artifact (C-10)."""
    root = tree({"pkg/app.py": "x = 1\n", ".gitignore": "generated/\n!\n"})
    adapter = StubAdapter(
        diagnostics=[Diag(kind="unresolved_call", path="pkg/app.py", line=1, message="no target")]
    )
    result, _, _ = analyze(root, out_dir, adapter)
    kinds = {row["kind"] for row in report(result, reports.DIAGNOSTICS_REPORT)["diagnostics"]}
    assert {"gitignore_problem", "unresolved_call"} <= kinds


# ---------------------------------------------------------------------------
# Output location (§5.1, FR-34)
# ---------------------------------------------------------------------------


def test_out_dir_derivation_follows_the_xdg_form(monkeypatch, tmp_path):
    """§5.1: `$XDG_DATA_HOME/pastapathfinder/<basename>-<sha256(abspath)[:12]>/`."""
    import hashlib

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    root = tmp_path / "codebase"
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:12]
    assert (
        runner.derive_out_dir(root) == tmp_path / "data" / "pastapathfinder" / f"codebase-{digest}"
    )


def test_out_dir_derivation_falls_back_to_the_local_share_default(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    derived = runner.derive_out_dir(tmp_path / "codebase")
    assert derived.parent == tmp_path / "home" / ".local" / "share" / "pastapathfinder"


def test_derived_output_is_outside_the_analyzed_tree(monkeypatch, tmp_path, tree):
    """§5.1: the tool never writes into the codebase and never discovers its own output."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    root = tree(SIMPLE_TREE)
    result = runner.run_analysis(
        root, adapters=[StubAdapter()], progress=ProgressSink(io.StringIO()), stdout=io.StringIO()
    )
    assert not result.out_dir.is_relative_to(root)
    assert result.index_path.is_file()


def test_config_can_redirect_the_output_directory(tree, tmp_path):
    elsewhere = tmp_path / "configured"
    root = tree(
        {"pkg/app.py": "x = 1\n", ".pastapathfinder.toml": f'[output]\ndir = "{elsewhere}"\n'}
    )
    result = runner.run_analysis(
        root, adapters=[StubAdapter()], progress=ProgressSink(io.StringIO()), stdout=io.StringIO()
    )
    assert result.out_dir == elsewhere


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_an_unwritable_out_directory_names_the_path_without_asking_for_elevation(tree, tmp_path):
    """AC-34.2: a permissions error identifying the path; no elevation request."""
    root = tree(SIMPLE_TREE)
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(stat.S_IREAD | stat.S_IEXEC)
    try:
        with pytest.raises(runner.OutputLocationError) as raised:
            analyze(root, locked / "out")
    finally:
        locked.chmod(stat.S_IRWXU)
    message = str(raised.value)
    assert str(locked / "out") in message
    assert not any(word in message.lower() for word in ("sudo", "administrator", "elevat"))


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_an_unwritable_report_location_stops_the_run_before_it_renders(tree, out_dir):
    """AC-7.3/AC-42.3: no human rendering is substituted for a missing structured report."""
    root = tree(SIMPLE_TREE)
    directory = reports.prepare_report_dir(out_dir)
    directory.chmod(stat.S_IREAD | stat.S_IEXEC)
    stdout = io.StringIO()
    try:
        with pytest.raises(reports.ReportWriteError) as raised:
            analyze(root, out_dir, stdout=stdout)
    finally:
        directory.chmod(stat.S_IRWXU)
    assert str(directory / reports.COVERAGE_REPORT) in str(raised.value)
    assert "Coverage:" not in stdout.getvalue()


# ---------------------------------------------------------------------------
# Progress (FR-41)
# ---------------------------------------------------------------------------


def test_progress_reaches_stderr_in_both_shapes(tree, out_dir):
    """AC-41.1 (`processed/total`) and AC-41.2 (activity when the total is unknown)."""
    root = tree(SIMPLE_TREE)
    _, stdout, stderr = analyze(root, out_dir)
    assert "discovering sources … 0s" in stderr  # total unknown during the walk
    assert "analyzing 2/2" in stderr
    assert "analyzing" not in stdout  # progress never contaminates the report channel


# ---------------------------------------------------------------------------
# Adapter dispatch (FR-23)
# ---------------------------------------------------------------------------


def test_a_candidate_no_adapter_claims_fails_by_name(tree, out_dir):
    """A file that reaches the adapter step unclaimed is never silently dropped."""
    root = tree(SIMPLE_TREE)
    with pytest.raises(runner.RunnerError) as raised:
        runner.run_analysis(
            root,
            out=out_dir,
            adapters=[],
            progress=ProgressSink(io.StringIO()),
            stdout=io.StringIO(),
        )
    assert "pkg/__init__.py" in str(raised.value)


def test_this_build_registers_the_python_adapter():
    """FR-23: v1's one language is registered, and it satisfies the §3.4 protocol."""
    (adapter,) = runner.default_adapters()
    assert isinstance(adapter, LanguageAdapter)
    assert adapter.language == "python"


def test_an_extensionless_python_script_is_claimed_on_its_shebang(tree, out_dir):
    """FR-1 rule (b) survives the hand-off: `recognizes()` sees the first line."""
    root = tree({"tools/runme": "#!/usr/bin/env python3\nprint(1)\n"})
    adapter = StubAdapter()
    result, _, _ = analyze(root, out_dir, adapter)
    assert [source.relpath for source in adapter.seen] == ["tools/runme"]
    assert result.counts["files_analyzed"] == 1


# ---------------------------------------------------------------------------
# Exit codes (FR-43, design.md §3.1)
# ---------------------------------------------------------------------------


def test_the_three_exit_codes_are_distinct_integers(monkeypatch, tree, tmp_path, capsys):
    """AC-43.1/43.2/43.3, asserted as three distinct integers from `main()` itself."""
    clean = tree({"pkg/app.py": "x = 1\n"}, name="clean")
    broken = tree({"pkg/app.py": "x = 1\n"}, name="broken")

    monkeypatch.setattr(runner, "default_adapters", lambda: (StubAdapter(),))
    success = cli.main(["analyze", str(clean), "--out", str(tmp_path / "out-clean")])

    monkeypatch.setattr(
        runner,
        "default_adapters",
        lambda: (StubAdapter(skip={"pkg/app.py": "line 1: invalid syntax"}),),
    )
    partial = cli.main(["analyze", str(broken), "--out", str(tmp_path / "out-broken")])

    failure = cli.main(["analyze", str(tmp_path / "nonesuch"), "--out", str(tmp_path / "out-none")])

    assert (success, partial, failure) == (0, 1, 2)
    assert len({success, partial, failure}) == 3
    assert "nonesuch" in capsys.readouterr().err


def test_exit_code_mapping_is_the_documented_one(tree, out_dir):
    result, _, _ = analyze(tree(SIMPLE_TREE), out_dir)
    assert cli.exit_code_for(result) == cli.EXIT_SUCCESS

    skipped = runner.RunResult(
        completed=True,
        root=result.root,
        out_dir=result.out_dir,
        index_path=result.index_path,
        reports_dir=result.reports_dir,
        counts=reports.coverage_counts(discovered=1, analyzed=0, skipped=1, excluded=0),
        run=result.run,
    )
    assert cli.exit_code_for(skipped) == cli.EXIT_PARTIAL

    incomplete = runner.RunResult(
        completed=False,
        root=result.root,
        out_dir=result.out_dir,
        index_path=result.index_path,
        reports_dir=result.reports_dir,
        counts=result.counts,
        run=result.run,
    )
    assert cli.exit_code_for(incomplete) == cli.EXIT_FAILURE
