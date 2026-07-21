"""The six §5.3 report documents, their writers, and their renderings.

design.md §3.10, §5.3, §5.4, D9, D17; requirements FR-5 (AC-5.1/5.3), FR-7 (AC-7.1-7.3),
FR-19 (AC-19.2), FR-35, FR-38, FR-42 (AC-42.1-42.4).
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from pastapathfinder import reports
from pastapathfinder.exclusions import ExclusionRecord
from pastapathfinder.schema import DEADCODE_CAVEAT, Diag


@pytest.fixture
def run() -> reports.RunInfo:
    return reports.RunInfo.start("0" * 32).finish(1.25)


def counts(discovered=3, analyzed=1, skipped=1, excluded=1) -> dict[str, int]:
    return reports.coverage_counts(
        discovered=discovered, analyzed=analyzed, skipped=skipped, excluded=excluded
    )


def all_documents(run: reports.RunInfo) -> dict[str, dict]:
    """One of each §5.3 document, in the shapes a clean run produces."""
    return {
        reports.COVERAGE_REPORT: reports.coverage_document(
            run,
            counts(),
            [
                reports.analyzed_row("pkg/app.py"),
                reports.skipped_row("pkg/broken.py", "line 3: invalid syntax"),
                reports.excluded_row(
                    ExclusionRecord("venv", True, "venv/", "default:python"),
                ),
            ],
        ),
        reports.EXCLUSIONS_REPORT: reports.exclusions_document(
            run, [ExclusionRecord("venv", True, "venv/", "default:python")]
        ),
        reports.REANALYSIS_REPORT: reports.reanalysis_document(run, mode=reports.MODE_FULL),
        reports.DIAGNOSTICS_REPORT: reports.diagnostics_document(
            run, [Diag(kind="probe_failure", path="odd", message="odd: unreadable")]
        ),
        reports.DEADCODE_REPORT: reports.deadcode_document(run, no_entry_points_warning=True),
        reports.CHANGE_WARNING_REPORT: reports.change_warning_document(run),
    }


# ---------------------------------------------------------------------------
# Format identity (FR-42)
# ---------------------------------------------------------------------------


def test_every_report_parses_and_carries_a_format_version(tmp_path, run):
    """AC-42.1: each report exists in the structured format and parses per its docs."""
    for filename, document in all_documents(run).items():
        path = reports.write_report(tmp_path, filename, document)
        parsed = json.loads(path.read_text(encoding="utf-8"))
        assert parsed["format_version"] == reports.FORMAT_VERSION, filename
        assert parsed[reports.RUN_BLOCK_KEY]["run_id"] == "0" * 32


def test_all_six_reports_are_named(run):
    """§5.3 lists six; a run writes all six (the C-10 convention)."""
    assert len(reports.REPORT_FILENAMES) == 6
    assert set(all_documents(run)) == set(reports.REPORT_FILENAMES)
    assert set(reports.RENDERERS) == set(reports.REPORT_FILENAMES)


def test_consumer_refuses_a_future_format_version(tmp_path, run):
    """AC-42.4: a consumer refuses a version it does not support rather than misreading."""
    document = dict(all_documents(run)[reports.COVERAGE_REPORT])
    document["format_version"] = 2
    path = reports.write_report(tmp_path, reports.COVERAGE_REPORT, document)

    with pytest.raises(reports.UnsupportedReportFormatError) as raised:
        reports.load_report(path)
    assert "2" in str(raised.value)
    assert str(reports.FORMAT_VERSION) in str(raised.value)

    # And the renderers refuse it too: no path reads a v2 document as if it were v1.
    with pytest.raises(reports.UnsupportedReportFormatError):
        reports.render(reports.COVERAGE_REPORT, document)


def test_missing_format_version_is_refused(tmp_path, run):
    document = {key: value for key, value in all_documents(run)[reports.DEADCODE_REPORT].items()}
    document.pop("format_version")
    path = reports.write_report(tmp_path, reports.DEADCODE_REPORT, document)
    with pytest.raises(reports.UnsupportedReportFormatError):
        reports.load_report(path)


def test_unparseable_report_is_an_error_not_a_guess(tmp_path):
    path = tmp_path / reports.COVERAGE_REPORT
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(reports.ReportReadError):
        reports.load_report(path)


def test_written_reports_are_byte_stable(tmp_path, run):
    """FR-44/D12: equal documents produce equal bytes, whatever the key insertion order."""
    document = all_documents(run)[reports.EXCLUSIONS_REPORT]
    shuffled = dict(reversed(list(document.items())))
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    first = reports.write_report(tmp_path / "a", reports.EXCLUSIONS_REPORT, document)
    second = reports.write_report(tmp_path / "b", reports.EXCLUSIONS_REPORT, shuffled)
    assert first.read_bytes() == second.read_bytes()


# ---------------------------------------------------------------------------
# Coverage reconciliation (FR-7, D17)
# ---------------------------------------------------------------------------


def test_counts_use_the_unit_explicit_names(run):
    """D17: the four §5.3 count names state their units in the data."""
    document = reports.coverage_document(run, counts(), [])
    assert set(document["counts"]) == {
        "entries_discovered",
        "files_analyzed",
        "files_skipped",
        "entries_excluded",
    }


def test_reconciliation_holds_from_the_counts_block_alone(run):
    """AC-7.1/AC-42.2: computable from four fields, without parsing anything else."""
    document = reports.coverage_document(
        run, counts(discovered=10, analyzed=6, skipped=2, excluded=2), []
    )
    block = document["counts"]
    assert (
        block["entries_discovered"]
        == block["files_analyzed"] + block["files_skipped"] + block["entries_excluded"]
    )


def test_a_mismatch_fails_loudly_before_anything_is_written(tmp_path, run):
    """AC-7.1: a deliberately injected mismatch fails the run; no report is produced."""
    bad = counts(discovered=9, analyzed=1, skipped=1, excluded=1)
    with pytest.raises(reports.CoverageMismatchError) as raised:
        reports.coverage_document(run, bad, [])
    message = str(raised.value)
    assert "entries_discovered=9" in message
    assert "AC-7.1" in message
    assert not list(tmp_path.iterdir())


def test_missing_counts_are_rejected(run):
    with pytest.raises(reports.CoverageMismatchError):
        reports.assert_coverage_reconciles({"entries_discovered": 0})


def test_coverage_rows_carry_status_reason_and_rule(run):
    """FR-7: exactly one status per entry; skipped rows carry a reason (AC-7.2)."""
    document = reports.coverage_document(
        run,
        counts(),
        [
            reports.analyzed_row("pkg/app.py"),
            reports.skipped_row("pkg/broken.py", "line 3: invalid syntax"),
            reports.excluded_row(ExclusionRecord("venv", True, "venv/", "default:python")),
        ],
    )
    rows = {row["path"]: row for row in document["files"]}
    assert rows["pkg/app.py"] == {"path": "pkg/app.py", "status": "analyzed", "is_dir": False}
    assert rows["pkg/broken.py"]["reason"] == "line 3: invalid syntax"
    assert rows["venv"]["is_dir"] is True
    assert rows["venv"]["rule"] == {"pattern": "venv/", "source": "default:python"}
    assert {row["status"] for row in document["files"]} <= set(reports.COVERAGE_STATUSES)


# ---------------------------------------------------------------------------
# Write failures (AC-7.3, AC-42.3, AC-34.2)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_unwritable_report_directory_names_the_location(tmp_path, run):
    """AC-7.3/AC-42.3: naming the location, never a silent omission."""
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(stat.S_IREAD | stat.S_IEXEC)
    try:
        with pytest.raises(reports.ReportWriteError) as raised:
            reports.prepare_report_dir(locked)
    finally:
        locked.chmod(stat.S_IRWXU)
    assert str(locked / reports.REPORTS_DIRNAME) in str(raised.value)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_write_failure_names_the_report_and_suggests_no_elevation(tmp_path, run):
    """AC-34.2: a permissions error names the path and never asks for elevation."""
    directory = reports.prepare_report_dir(tmp_path)
    directory.chmod(stat.S_IREAD | stat.S_IEXEC)
    try:
        with pytest.raises(reports.ReportWriteError) as raised:
            reports.write_report(directory, reports.COVERAGE_REPORT, {"format_version": 1})
    finally:
        directory.chmod(stat.S_IRWXU)
    message = str(raised.value).lower()
    assert str(directory / reports.COVERAGE_REPORT) in str(raised.value)
    assert not any(word in message for word in ("sudo", "administrator", "elevat", "as root"))


# ---------------------------------------------------------------------------
# Renderings (D9: derived from the parsed structured form)
# ---------------------------------------------------------------------------


def test_coverage_rendering_reports_the_arithmetic_and_the_skips(run):
    document = reports.coverage_document(
        run, counts(), [reports.skipped_row("pkg/broken.py", "line 3: invalid syntax")]
    )
    text = reports.render_coverage(document)
    assert "3 discovered = 1 analyzed + 1 skipped + 1 excluded" in text
    assert "pkg/broken.py: line 3: invalid syntax" in text


def test_coverage_rendering_states_when_nothing_was_discovered(run):
    """EC-10: an empty root says so instead of reporting a successful analysis of nothing."""
    document = reports.coverage_document(
        run, counts(discovered=0, analyzed=0, skipped=0, excluded=0), []
    )
    assert "No recognized Python sources were discovered." in reports.render_coverage(document)


def test_coverage_rendering_states_when_nothing_was_analyzed(run):
    """AC-6.2: every file failed to parse — the output says so explicitly."""
    document = reports.coverage_document(
        run,
        counts(discovered=2, analyzed=0, skipped=2, excluded=0),
        [
            reports.skipped_row("a.py", "invalid syntax"),
            reports.skipped_row("b.py", "invalid syntax"),
        ],
    )
    assert "No files were analyzed" in reports.render_coverage(document)


def test_exclusion_rendering_names_each_rule(run):
    """AC-5.1: an excluded path appears with the rule that excluded it."""
    document = reports.exclusions_document(
        run,
        [
            ExclusionRecord("venv", True, "venv/", "default:python"),
            ExclusionRecord("gen/pb2.py", False, "*.pb2.py", "user:exclude"),
        ],
    )
    text = reports.render_exclusions(document)
    assert "venv/ — venv/ (default:python)" in text
    assert "gen/pb2.py — *.pb2.py (user:exclude)" in text


def test_exclusion_free_run_still_reports_the_absence(run):
    """AC-5.3: the report is produced and states that nothing was excluded."""
    document = reports.exclusions_document(run, [])
    assert document["none_excluded"] is True
    assert document["exclusions"] == []
    assert "none" in reports.render_exclusions(document)


def test_deadcode_carries_the_caveat_in_both_forms(run):
    """AC-19.2: the caveat is present in the artifact and in the rendering, verbatim."""
    document = reports.deadcode_document(run, no_entry_points_warning=True)
    assert document["caveat"] == DEADCODE_CAVEAT
    assert DEADCODE_CAVEAT in reports.render_deadcode(document)


def test_deadcode_rendering_warns_when_no_entry_points_were_detected(run):
    """AC-19.3: no entry points means uninformative, not "the codebase is dead"."""
    text = reports.render_deadcode(reports.deadcode_document(run, no_entry_points_warning=True))
    assert "no entry points were detected" in text
    assert "must not be read as dead code" in text


def test_change_warning_note_is_never_a_guarantee(run):
    """FR-38: the fixed note states best-effort; a test pins it so it cannot drift."""
    document = reports.change_warning_document(run)
    assert document["note"] == reports.CHANGE_WARNING_NOTE
    assert "not a guarantee" in document["note"].lower()
    assert "does not prove" in document["note"].lower()


def test_change_warning_renders_nothing_when_nothing_changed(run):
    """AC-38.2: an unchanged run emits empty lists and no warning line."""
    document = reports.change_warning_document(run)
    assert document["changed"] == document["removed"] == document["check_failures"] == []
    assert reports.render_change_warning(document) == ""


def test_change_warning_renders_the_note_when_something_changed(run):
    document = reports.change_warning_document(
        run,
        changed=["pkg/app.py"],
        removed=["pkg/gone.py"],
        check_failures=[{"path": "pkg/locked.py", "error": "Permission denied"}],
    )
    text = reports.render_change_warning(document)
    assert "changed: pkg/app.py" in text
    assert "removed: pkg/gone.py" in text
    assert "could not be checked: pkg/locked.py" in text
    assert reports.CHANGE_WARNING_NOTE in text


def test_reanalysis_modes_and_reasons_are_constrained(run):
    with pytest.raises(reports.ReportError):
        reports.reanalysis_document(run, mode="nonesuch")
    with pytest.raises(reports.ReportError):
        reports.reanalysis_document(
            run, mode=reports.MODE_INCREMENTAL, reprocessed=[{"path": "a.py", "reason": "vibes"}]
        )


def test_reanalysis_no_change_rendering_states_the_no_op(run):
    """AC-35.2: a run with nothing changed says that no files were re-processed."""
    document = reports.reanalysis_document(run, mode=reports.MODE_SKIPPED_NO_CHANGES)
    assert "no files were re-processed" in reports.render_reanalysis(document).lower()


def test_diagnostics_are_sorted_and_summarized(run):
    document = reports.diagnostics_document(
        run,
        [
            Diag(kind="symlink_skip", path="z.py", message="outside the root"),
            Diag(kind="probe_failure", path="a", message="unreadable"),
        ],
    )
    assert [row["path"] for row in document["diagnostics"]] == ["a", "z.py"]
    text = reports.render_diagnostics(document)
    assert "Diagnostics: 2 (probe_failure 1, symlink_skip 1)" in text


def test_clean_run_diagnostics_are_present_and_empty(run):
    """The C-10 convention: the artifact exists even when there is nothing to say."""
    document = reports.diagnostics_document(run, [])
    assert document["diagnostics"] == []
    assert reports.render_diagnostics(document) == "Diagnostics: none."


def test_run_block_must_be_finished_before_a_report_is_built():
    unfinished = reports.RunInfo.start("a" * 32)
    with pytest.raises(ValueError):
        unfinished.as_json()


def test_run_block_holds_exactly_the_volatile_fields(run):
    """§5.4: `run` is the reports' whole volatile register — no more, no less."""
    assert set(run.as_json()) == {"run_id", "started_at", "finished_at", "duration_seconds"}


def test_report_paths_are_the_documented_names():
    assert Path(reports.COVERAGE_REPORT).suffix == ".json"
    assert reports.REPORT_FILENAMES == (
        "coverage.json",
        "exclusions.json",
        "reanalysis.json",
        "diagnostics.json",
        "deadcode.json",
        "change_warning.json",
    )
