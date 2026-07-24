"""The owed user-facing documentation set (task 5.3).

design.md §6 (the owed docs), §5.1, §5.3, §5.4, §5.5; requirements FR-31, FR-32, FR-42
(AC-42.1/42.4), FR-43, FR-4.

Two things are proven here:

1. **`report-formats.md` describes the reports the tool actually writes.** A fixture
   `analyze` run produces all six reports, and each is validated field-for-field against
   the schema `docs/report-formats.md` publishes. If the writer and the doc drift apart,
   this fails — the doc is the FR-42/AC-42.4 contract, so it has to track the code.
2. **The other owed docs cover the points their requirements name** — the single install
   command (AC-32.1), the FR-31 WSL Linux-filesystem condition, the `.pastapathfinder.toml`
   surface and the `--out` default derivation (§5.5, §5.1), and the three exit codes
   (FR-43).
"""

from __future__ import annotations

import io
from pathlib import Path

from pastapathfinder import reports, runner
from pastapathfinder.progress import ProgressSink
from stub_adapter import StubAdapter

DOCS = Path(__file__).resolve().parents[2] / "docs"

# A tree with an analyzed file, a non-source file, and a default-excluded directory, so the
# coverage and exclusion reports carry rows of every kind the schema allows.
FIXTURE_TREE = {
    "pkg/__init__.py": "",
    "pkg/app.py": "def main():\n    return 1\n",
    "README.md": "not a source file\n",
    "venv/lib/thing.py": "x = 1\n",
}


def _analyze(root: Path, out: Path):
    result = runner.run_analysis(
        root,
        out=out,
        adapters=[StubAdapter()],
        progress=ProgressSink(io.StringIO(), interval=0.0),
        stdout=io.StringIO(),
    )
    return result


# ---------------------------------------------------------------------------
# Schema validators — a direct transcription of docs/report-formats.md (§5.3)
# ---------------------------------------------------------------------------

_RUN_FIELDS = {"run_id", "started_at", "finished_at", "duration_seconds"}


def _check_envelope(doc: dict) -> None:
    assert doc["format_version"] == 1, "every report carries format_version 1 (AC-42.4)"
    run = doc["run"]
    assert set(run) == _RUN_FIELDS, f"run block fields: {set(run)}"


def _check_coverage(doc: dict) -> None:
    _check_envelope(doc)
    counts = doc["counts"]
    assert set(counts) == {
        "entries_discovered",
        "files_analyzed",
        "files_skipped",
        "entries_excluded",
    }
    assert all(isinstance(v, int) for v in counts.values())
    assert counts["entries_discovered"] == (
        counts["files_analyzed"] + counts["files_skipped"] + counts["entries_excluded"]
    )
    for row in doc["files"]:
        assert isinstance(row["path"], str)
        assert row["status"] in {"analyzed", "skipped", "excluded"}
        assert isinstance(row["is_dir"], bool)
        if row["status"] == "skipped":
            assert isinstance(row["reason"], str)
        if row["status"] == "excluded":
            assert set(row["rule"]) == {"pattern", "source"}


def _check_exclusions(doc: dict) -> None:
    _check_envelope(doc)
    assert isinstance(doc["none_excluded"], bool)
    for row in doc["exclusions"]:
        assert set(row) == {"path", "is_dir", "pattern", "source"}
        assert row["source"] in {"default:common", "default:python", "user:exclude"} or row[
            "source"
        ].startswith("gitignore:")


def _check_reanalysis(doc: dict) -> None:
    _check_envelope(doc)
    assert doc["mode"] in {"full", "incremental", "skipped_no_changes", "fallback"}
    for row in doc["reprocessed"]:
        assert set(row) == {"path", "reason"}
        assert row["reason"] in {"content_changed", "dependent", "cache_fallback"}
    assert all(isinstance(p, str) for p in doc["removed"])


def _check_change_warning(doc: dict) -> None:
    _check_envelope(doc)
    assert isinstance(doc["note"], str) and doc["note"]
    assert all(isinstance(p, str) for p in doc["changed"])
    assert all(isinstance(p, str) for p in doc["removed"])
    for row in doc["check_failures"]:
        assert set(row) == {"path", "error"}


def _check_diagnostics(doc: dict) -> None:
    _check_envelope(doc)
    for diag in doc["diagnostics"]:
        assert set(diag) == {"kind", "path", "line", "col", "message", "extra"}
        assert isinstance(diag["kind"], str)
        assert isinstance(diag["message"], str)


def _check_deadcode(doc: dict) -> None:
    _check_envelope(doc)
    assert isinstance(doc["caveat"], str) and doc["caveat"]
    assert isinstance(doc["no_entry_points_warning"], bool)
    for group in doc["unreachable"]:
        assert isinstance(group["file"], str)
        for fn in group["functions"]:
            assert set(fn) == {"id", "name", "start_line"}


_VALIDATORS = {
    reports.COVERAGE_REPORT: _check_coverage,
    reports.EXCLUSIONS_REPORT: _check_exclusions,
    reports.REANALYSIS_REPORT: _check_reanalysis,
    reports.CHANGE_WARNING_REPORT: _check_change_warning,
    reports.DIAGNOSTICS_REPORT: _check_diagnostics,
    reports.DEADCODE_REPORT: _check_deadcode,
}


def test_every_report_matches_its_documented_schema(tree, out_dir):
    """AC-42.1/42.4: each report a run produces parses per the documented format."""
    root = tree(FIXTURE_TREE)
    result = _analyze(root, out_dir)

    # All six §5.3 reports exist and each has a validator (the doc describes all of them).
    assert set(result.report_paths) == set(reports.REPORT_FILENAMES)
    assert set(_VALIDATORS) == set(reports.REPORT_FILENAMES)

    for filename, validator in _VALIDATORS.items():
        doc = reports.load_report(result.report_paths[filename])
        validator(doc)


# ---------------------------------------------------------------------------
# The owed docs exist and cover the points their requirements name
# ---------------------------------------------------------------------------


def _doc(name: str) -> str:
    return (DOCS / name).read_text(encoding="utf-8")


def test_the_owed_doc_set_is_present():
    for name in (
        "install.md",
        "configuration.md",
        "wsl.md",
        "report-formats.md",
        "exit-codes.md",
    ):
        assert (DOCS / name).is_file(), f"docs/{name} is owed by design.md §6"


def test_install_documents_the_single_command_and_offline_posture():
    text = _doc("install.md").lower()
    # AC-32.1: a single pip install command. The tool is not on PyPI, so the documented
    # command installs from a checkout of the source tree.
    assert "pip install ." in text
    # FR-33/FR-34: offline and no elevated privileges.
    assert "network" in text
    assert "privilege" in text or "admin" in text


def test_wsl_states_the_fr31_linux_filesystem_condition():
    text = _doc("wsl.md")
    assert "WSL2" in text
    assert "/mnt/c" in text
    # The performance bounds apply only on the Linux filesystem (FR-31).
    lower = text.lower()
    assert "linux filesystem" in lower
    assert "fr-29" in lower and "fr-30" in lower
    assert "not asserted" in lower


def test_configuration_documents_the_toml_surface_and_out_derivation():
    text = _doc("configuration.md")
    assert ".pastapathfinder.toml" in text
    # §5.5 surface.
    for token in ("[exclude]", "add", "reinclude", "[output]", "dir"):
        assert token in text
    # §5.1 default output derivation and its precedence.
    assert "XDG_DATA_HOME" in text
    assert "sha256" in text
    assert "--out" in text


def test_exit_codes_documents_the_three_codes():
    text = _doc("exit-codes.md")
    for code in ("0", "1", "2"):
        assert f"`{code}`" in text
    lower = text.lower()
    assert "success" in lower
    assert "partial" in lower
    assert "fr-43" in lower


def test_report_formats_publishes_all_six_schemas_and_the_volatile_register():
    text = _doc("report-formats.md")
    for filename in reports.REPORT_FILENAMES:
        assert filename in text, f"{filename} schema is owed in report-formats.md"
    # §5.4 volatile-field register, mirrored here.
    assert "meta.created_at" in text
    assert "meta.run_id" in text
    assert "format_version" in text
