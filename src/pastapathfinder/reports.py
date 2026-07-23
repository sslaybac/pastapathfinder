"""The six JSON reports of design.md §5.3 and their human-readable renderings.

design.md §3.10 (`reports`), §5.3 (the schemas, normative), §5.4 (volatile fields), D9,
D17; requirements FR-5 (AC-5.1-5.3), FR-7 (AC-7.1-7.3), FR-35, FR-38, FR-42
(AC-42.1-42.4), FR-34 (AC-34.2), EC-8, EC-10.

D9's discipline, which shapes this whole module: **the structured document is
authoritative and the human rendering is derived from it.** Every report is built as a
plain `dict`, written as JSON, and re-read before it is rendered — so a rendering can
never describe something the file does not say, and a failed write can never be papered
over with a printed summary (AC-42.3).

Every document carries `format_version` (AC-42.4). `load_report()` is the reference
consumer: it refuses a version it does not support rather than misreading it, which is
the v1 insurance for the report evolution FR-42 anticipates.

The `run` block (`run_id`, `started_at`, `finished_at`, `duration_seconds`) is the
reports' half of §5.4's volatile-field register: it is the only content two runs over
identical input are permitted to differ in (FR-44). Everything else this module writes is
derived deterministically from the run's inputs, and lists are ordered canonically here
rather than in the order things happened to be produced.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from pastapathfinder.exclusions import ExclusionRecord
from pastapathfinder.schema import DEADCODE_CAVEAT, Diag

# ---------------------------------------------------------------------------
# Format identity (FR-42, AC-42.4)
# ---------------------------------------------------------------------------

#: The version stamped into every report. A change to any §5.3 schema that a v1 consumer
#: could misread is a bump here, not a silent edit (AC-42.4; backlog B-8 is the
#: anticipated first case).
FORMAT_VERSION = 1

#: Reports live in their own subdirectory of the output tree, overwritten each run.
REPORTS_DIRNAME = "reports"

COVERAGE_REPORT = "coverage.json"
EXCLUSIONS_REPORT = "exclusions.json"
REANALYSIS_REPORT = "reanalysis.json"
CHANGE_WARNING_REPORT = "change_warning.json"
DIAGNOSTICS_REPORT = "diagnostics.json"
DEADCODE_REPORT = "deadcode.json"

#: Every report of §5.3, in the order a run writes and prints them.
REPORT_FILENAMES: tuple[str, ...] = (
    COVERAGE_REPORT,
    EXCLUSIONS_REPORT,
    REANALYSIS_REPORT,
    DIAGNOSTICS_REPORT,
    DEADCODE_REPORT,
    CHANGE_WARNING_REPORT,
)

#: §5.4's volatile block, reports half. Named here because the FR-44 comparator (task
#: 4.3) strips exactly this key and nothing else.
RUN_BLOCK_KEY = "run"

# ---------------------------------------------------------------------------
# Vocabularies (§5.3)
# ---------------------------------------------------------------------------

#: FR-7's three statuses. Their reconciliation is `assert_coverage_reconciles()`.
STATUS_ANALYZED = "analyzed"
STATUS_SKIPPED = "skipped"
STATUS_EXCLUDED = "excluded"
COVERAGE_STATUSES: tuple[str, ...] = (STATUS_ANALYZED, STATUS_SKIPPED, STATUS_EXCLUDED)

#: D17's unit-explicit count names. `entries_*` count *entries* — and a pruned directory
#: is one entry (§8-O1) — while `files_*` count files; the names say which so no consumer
#: has to consult prose to read the arithmetic (AC-42.2).
COUNT_DISCOVERED = "entries_discovered"
COUNT_ANALYZED = "files_analyzed"
COUNT_SKIPPED = "files_skipped"
COUNT_EXCLUDED = "entries_excluded"
COUNT_KEYS: tuple[str, ...] = (COUNT_DISCOVERED, COUNT_ANALYZED, COUNT_SKIPPED, COUNT_EXCLUDED)

#: §5.3's `reanalysis.json` modes.
MODE_FULL = "full"
MODE_INCREMENTAL = "incremental"
MODE_SKIPPED_NO_CHANGES = "skipped_no_changes"
MODE_FALLBACK = "fallback"
REANALYSIS_MODES: tuple[str, ...] = (
    MODE_FULL,
    MODE_INCREMENTAL,
    MODE_SKIPPED_NO_CHANGES,
    MODE_FALLBACK,
)

#: FR-35's three attributions, exhaustive: every re-processed file carries exactly one.
#: `content_changed` — the file's own bytes moved; `dependent` — re-resolved only because a
#: dependency did; `cache_fallback` — reprocessed by the AC-24.3 wipe-and-rebuild.
REASON_CONTENT_CHANGED = "content_changed"
REASON_DEPENDENT = "dependent"
REASON_CACHE_FALLBACK = "cache_fallback"
REPROCESS_REASONS: tuple[str, ...] = (
    REASON_CONTENT_CHANGED,
    REASON_DEPENDENT,
    REASON_CACHE_FALLBACK,
)

#: FR-38's fixed `note`. The wording is load-bearing: the post-run check narrows the
#: window in which a mid-run edit goes unnoticed and cannot close it, so the report must
#: never read as a freshness guarantee.
CHANGE_WARNING_NOTE = (
    "Best effort, not a guarantee. This check compares the contents this run read "
    "against the files as they are now; a change made after the check cannot be seen. "
    "The absence of a warning does not prove the results are current."
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ReportError(Exception):
    """Base class for every report failure. `cli.main()` maps these to exit 2 (D10)."""


class ReportWriteError(ReportError):
    """A report could not be written; the run terminates (AC-7.3, AC-42.3, AC-34.2).

    The message names the location and the reason, and never suggests re-running with
    elevated privileges — FR-34 forbids the tool from requiring them, so proposing them
    would be advice against its own design.
    """


class ReportReadError(ReportError):
    """A report exists but cannot be parsed (AC-42.1's failure side)."""


class UnsupportedReportFormatError(ReportError):
    """A report carries a `format_version` this build does not support (AC-42.4)."""

    def __init__(self, path: Path | str, found: object) -> None:
        self.path = Path(path)
        self.found = found
        self.supported = FORMAT_VERSION
        found_text = "no format_version" if found is None else repr(found)
        super().__init__(
            f"report {self.path}: found {found_text}, supported format_version "
            f"{self.supported!r}; this build refuses to read it rather than "
            f"misinterpreting it"
        )


class CoverageMismatchError(ReportError):
    """FR-7's reconciliation does not hold — a pipeline bug, never a user error.

    AC-7.1 is checked *before* `coverage.json` is written, so a run whose accounting is
    wrong fails loudly instead of publishing a report that quietly hides files.
    """


# ---------------------------------------------------------------------------
# The volatile run block (§5.4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunInfo:
    """Provenance for one run: the `run*` block of every §5.3 report.

    Volatile by design (§5.4) — these four fields are what two otherwise-identical runs
    are allowed to differ in, and the FR-44 comparator strips them before diffing.
    """

    run_id: str
    started_at: str
    finished_at: str | None = None
    duration_seconds: float | None = None

    @classmethod
    def start(cls, run_id: str | None = None, *, now: datetime | None = None) -> RunInfo:
        moment = now or datetime.now(UTC)
        return cls(run_id=run_id or uuid4().hex, started_at=_isoformat(moment))

    def finish(self, duration_seconds: float, *, now: datetime | None = None) -> RunInfo:
        moment = now or datetime.now(UTC)
        return RunInfo(
            run_id=self.run_id,
            started_at=self.started_at,
            finished_at=_isoformat(moment),
            duration_seconds=round(float(duration_seconds), 3),
        )

    def as_json(self) -> dict[str, Any]:
        if self.finished_at is None or self.duration_seconds is None:
            raise ValueError("RunInfo.finish() must be called before the reports are built")
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": self.duration_seconds,
        }


def _isoformat(moment: datetime) -> str:
    """UTC, second precision — a timestamp a human and a parser can both read."""
    return moment.astimezone(UTC).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# Row helpers (one definition site per §5.3 row shape)
# ---------------------------------------------------------------------------


def analyzed_row(path: str) -> dict[str, Any]:
    """A coverage row for a successfully analyzed file."""
    return {"path": path, "status": STATUS_ANALYZED, "is_dir": False}


def skipped_row(path: str, reason: str) -> dict[str, Any]:
    """A coverage row for a skipped file; `reason` is AC-7.2's human-readable text."""
    return {"path": path, "status": STATUS_SKIPPED, "is_dir": False, "reason": reason}


def excluded_row(record: ExclusionRecord) -> dict[str, Any]:
    """A coverage row for an excluded entry, carrying the rule that excluded it (FR-7).

    Per D17/§8-O1 an excluded directory is *one* entry and its contents are never
    enumerated, so `is_dir` is what tells a reader whether this row stands for one file
    or a pruned subtree.
    """
    return {
        "path": record.path,
        "status": STATUS_EXCLUDED,
        "is_dir": record.is_dir,
        "rule": {"pattern": record.pattern, "source": record.source},
    }


def _diag_json(diagnostic: Diag) -> dict[str, Any]:
    return {
        "kind": diagnostic.kind,
        "path": diagnostic.path,
        "line": diagnostic.line,
        "col": diagnostic.col,
        "message": diagnostic.message,
        "extra": dict(diagnostic.extra),
    }


def _diag_sort_key(entry: Mapping[str, Any]) -> tuple[str, int, int, str, str]:
    """A total order over diagnostics, so their file order never depends on timing."""
    return (
        str(entry.get("path") or ""),
        int(entry.get("line") or 0),
        int(entry.get("col") or 0),
        str(entry.get("kind") or ""),
        str(entry.get("message") or ""),
    )


# ---------------------------------------------------------------------------
# Documents (design.md §5.3, normative shapes)
# ---------------------------------------------------------------------------


def _document(run: RunInfo, body: Mapping[str, Any]) -> dict[str, Any]:
    return {"format_version": FORMAT_VERSION, RUN_BLOCK_KEY: run.as_json(), **body}


def assert_coverage_reconciles(counts: Mapping[str, int]) -> None:
    """AC-7.1/AC-42.2: `entries_discovered = files_analyzed + files_skipped + entries_excluded`.

    Computable from the four `counts` fields alone — which is exactly what AC-42.2 asks
    of a consumer, so the pipeline checks itself the way a consumer would.
    """
    missing = [key for key in COUNT_KEYS if key not in counts]
    if missing:
        raise CoverageMismatchError(
            f"coverage counts are missing {missing}; §5.3 requires all four"
        )
    parts = counts[COUNT_ANALYZED] + counts[COUNT_SKIPPED] + counts[COUNT_EXCLUDED]
    if counts[COUNT_DISCOVERED] != parts:
        raise CoverageMismatchError(
            f"coverage does not reconcile (FR-7/AC-7.1): {COUNT_DISCOVERED}="
            f"{counts[COUNT_DISCOVERED]} but {COUNT_ANALYZED}({counts[COUNT_ANALYZED]}) + "
            f"{COUNT_SKIPPED}({counts[COUNT_SKIPPED]}) + {COUNT_EXCLUDED}"
            f"({counts[COUNT_EXCLUDED]}) = {parts}; this is a pipeline defect and the run "
            f"cannot publish a coverage report that hides the difference"
        )


def coverage_counts(
    *, discovered: int, analyzed: int, skipped: int, excluded: int
) -> dict[str, int]:
    """The §5.3 `counts` block, with D17's unit-explicit names."""
    return {
        COUNT_DISCOVERED: discovered,
        COUNT_ANALYZED: analyzed,
        COUNT_SKIPPED: skipped,
        COUNT_EXCLUDED: excluded,
    }


def coverage_document(
    run: RunInfo, counts: Mapping[str, int], files: Iterable[Mapping[str, Any]]
) -> dict[str, Any]:
    """`coverage.json` (FR-7, FR-42; D17's field names).

    The reconciliation is asserted here — before any bytes are written — so an
    inconsistent run fails rather than publishing the inconsistency (AC-7.1).
    """
    assert_coverage_reconciles(counts)
    rows = sorted(files, key=lambda row: (str(row["path"]), str(row["status"])))
    return _document(run, {"counts": {key: counts[key] for key in COUNT_KEYS}, "files": rows})


def exclusions_document(run: RunInfo, exclusions: Iterable[ExclusionRecord]) -> dict[str, Any]:
    """`exclusions.json` (FR-5): every excluded path with the rule that excluded it."""
    rows = [
        {
            "path": record.path,
            "is_dir": record.is_dir,
            "pattern": record.pattern,
            "source": record.source,
        }
        for record in sorted(exclusions, key=lambda record: record.path)
    ]
    # AC-5.3: an exclusion-free run still produces the report and says so explicitly.
    return _document(run, {"exclusions": rows, "none_excluded": not rows})


def reanalysis_document(
    run: RunInfo,
    *,
    mode: str,
    reprocessed: Iterable[Mapping[str, str]] = (),
    removed: Iterable[str] = (),
) -> dict[str, Any]:
    """`reanalysis.json` (FR-35).

    Populated by `incremental` (task 4.1); written on every run under the C-10
    convention, so a full run states `mode: full` with empty lists rather than omitting
    the artifact.
    """
    if mode not in REANALYSIS_MODES:
        raise ReportError(
            f"unknown re-analysis mode {mode!r}; §5.3 defines {list(REANALYSIS_MODES)}"
        )
    rows = []
    for entry in reprocessed:
        reason = entry["reason"]
        if reason not in REPROCESS_REASONS:
            raise ReportError(
                f"unknown re-analysis reason {reason!r}; FR-35 defines {list(REPROCESS_REASONS)}"
            )
        rows.append({"path": entry["path"], "reason": reason})
    return _document(
        run,
        {
            "mode": mode,
            "reprocessed": sorted(rows, key=lambda row: row["path"]),
            "removed": sorted(removed),
        },
    )


def change_warning_document(
    run: RunInfo,
    *,
    changed: Iterable[str] = (),
    removed: Iterable[str] = (),
    check_failures: Iterable[Mapping[str, str]] = (),
) -> dict[str, Any]:
    """`change_warning.json` (FR-38). Populated by `postrun` (task 4.2)."""
    failures = [
        {"path": entry["path"], "error": entry["error"]}
        for entry in sorted(check_failures, key=lambda entry: entry["path"])
    ]
    return _document(
        run,
        {
            "note": CHANGE_WARNING_NOTE,
            "changed": sorted(changed),
            "removed": sorted(removed),
            "check_failures": failures,
        },
    )


def diagnostics_document(run: RunInfo, diagnostics: Iterable[Diag]) -> dict[str, Any]:
    """`diagnostics.json` — the C-10 per-run anomaly artifact, empty-listed when clean."""
    rows = sorted((_diag_json(entry) for entry in diagnostics), key=_diag_sort_key)
    return _document(run, {"diagnostics": rows})


def deadcode_document(
    run: RunInfo,
    *,
    no_entry_points_warning: bool,
    unreachable: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """`deadcode.json` (FR-19). Populated by `queries.dead_code()` (task 3.4).

    The caveat is written into the document itself, not left to renderers: AC-19.2 wants
    it present in *every* presentation, and the only way to guarantee that is for the
    authoritative artifact to carry it.
    """
    return _document(
        run,
        {
            "caveat": DEADCODE_CAVEAT,
            "no_entry_points_warning": bool(no_entry_points_warning),
            "unreachable": sorted(unreachable, key=lambda group: str(group["file"])),
        },
    )


# ---------------------------------------------------------------------------
# Writing and reading
# ---------------------------------------------------------------------------


def prepare_report_dir(out_dir: Path | str) -> Path:
    """Create `<out>/reports/`, failing the run by name if it cannot be created.

    Called before any analysis so an unusable output location costs a walk of nothing
    (AC-34.2, AC-7.3).
    """
    directory = Path(out_dir) / REPORTS_DIRNAME
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ReportWriteError(
            f"cannot create the report directory {directory}: {exc.strerror or exc}; "
            f"choose a writable location with --out or [output] dir"
        ) from exc
    return directory


def write_report(directory: Path | str, filename: str, document: Mapping[str, Any]) -> Path:
    """Write one report as JSON, returning its path (AC-42.1).

    Keys are sorted and the encoding is fixed, so two runs producing equal documents
    produce byte-identical files (FR-44/D12).
    """
    path = Path(directory) / filename
    try:
        text = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError) as exc:  # pragma: no cover - a producer bug
        raise ReportError(f"report {filename} is not JSON-serializable: {exc}") from exc
    try:
        path.write_text(f"{text}\n", encoding="utf-8")
    except OSError as exc:
        # AC-7.3/AC-42.3: the run terminates naming the location. It does not fall back
        # to printing a summary — a rendering is never a substitute for the report.
        raise ReportWriteError(
            f"cannot write the report {path}: {exc.strerror or exc}; "
            f"the run cannot complete without it"
        ) from exc
    return path


def load_report(path: Path | str) -> dict[str, Any]:
    """Parse a report, refusing an unsupported `format_version` (AC-42.4).

    This is the reference consumer: the run's own renderings go through it, so the
    refusal path is exercised by every run rather than only by a future reader.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReportReadError(f"cannot read the report {path}: {exc.strerror or exc}") from exc
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReportReadError(f"report {path} is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise ReportReadError(f"report {path} is not a JSON object")
    require_supported(document, path)
    return document


def require_supported(document: Mapping[str, Any], path: Path | str = "<document>") -> None:
    """Raise unless `document` carries a `format_version` this build supports."""
    version = document.get("format_version")
    if version != FORMAT_VERSION:
        raise UnsupportedReportFormatError(path, version)


# ---------------------------------------------------------------------------
# Human-readable renderings (D9: derived from the parsed structured form)
# ---------------------------------------------------------------------------

#: How many rows a rendering lists before deferring to the report file. The renderings
#: are a summary for a human at the end of a run; the JSON is the complete record.
RENDER_LIMIT = 10


def _overflow(rows: Sequence[Any], filename: str) -> list[str]:
    if len(rows) <= RENDER_LIMIT:
        return []
    return [f"  … and {len(rows) - RENDER_LIMIT} more (see {filename})"]


def render_coverage(document: Mapping[str, Any]) -> str:
    """FR-7's coverage summary, including EC-10's and AC-6.2's explicit statements."""
    require_supported(document)
    counts = document["counts"]
    discovered = counts[COUNT_DISCOVERED]
    analyzed = counts[COUNT_ANALYZED]
    skipped = counts[COUNT_SKIPPED]
    excluded = counts[COUNT_EXCLUDED]
    lines = [
        f"Coverage: {discovered} discovered = {analyzed} analyzed + {skipped} skipped "
        f"+ {excluded} excluded"
    ]
    if analyzed + skipped == 0:
        # EC-10: an empty root, or one with no recognized sources, says so rather than
        # reporting a successful analysis of nothing.
        lines.append("  No recognized Python sources were discovered.")
    elif analyzed == 0:
        # AC-6.2: every file failed; the run completed but analyzed nothing.
        lines.append("  No files were analyzed: every discovered source was skipped.")
    skips = [row for row in document["files"] if row["status"] == STATUS_SKIPPED]
    for row in skips[:RENDER_LIMIT]:
        lines.append(f"  skipped {row['path']}: {row.get('reason', 'no reason recorded')}")
    lines.extend(_overflow(skips, COVERAGE_REPORT))
    return "\n".join(lines)


def render_exclusions(document: Mapping[str, Any]) -> str:
    """FR-5's exclusion summary: every path with the rule that excluded it."""
    require_supported(document)
    rows = document["exclusions"]
    if document["none_excluded"]:
        # AC-5.3: the report exists and states that nothing was excluded.
        return "Exclusions: none — no exclusion rule matched anything in this codebase."
    lines = [f"Exclusions: {len(rows)} entries excluded"]
    for row in rows[:RENDER_LIMIT]:
        marker = "/" if row["is_dir"] else ""
        lines.append(f"  {row['path']}{marker} — {row['pattern']} ({row['source']})")
    lines.extend(_overflow(rows, EXCLUSIONS_REPORT))
    return "\n".join(lines)


def render_reanalysis(document: Mapping[str, Any]) -> str:
    """FR-35's re-analysis summary, including AC-35.2's explicit no-op statement."""
    require_supported(document)
    mode = document["mode"]
    reprocessed = document["reprocessed"]
    removed = document["removed"]
    if mode == MODE_SKIPPED_NO_CHANGES:
        return "Re-analysis: nothing changed — no files were re-processed."
    if mode == MODE_FULL:
        # A full run re-derives the whole codebase; the per-file delta lives in
        # coverage.json, so this states the mode rather than an incremental file count.
        lines = ["Re-analysis: full analysis of the codebase."]
    else:
        lines = [f"Re-analysis ({mode}): {len(reprocessed)} files re-processed"]
    for row in reprocessed[:RENDER_LIMIT]:
        lines.append(f"  {row['path']} — {row['reason']}")
    lines.extend(_overflow(reprocessed, REANALYSIS_REPORT))
    if removed:
        lines.append(f"  removed since the last run: {len(removed)}")
    return "\n".join(lines)


def render_change_warning(document: Mapping[str, Any]) -> str:
    """FR-38's warning. Empty string when nothing changed (AC-38.2: no warning line)."""
    require_supported(document)
    changed = document["changed"]
    removed = document["removed"]
    failures = document["check_failures"]
    if not (changed or removed or failures):
        return ""
    lines = ["Warning: files changed while this run was in progress; re-analyze to refresh."]
    for path in changed[:RENDER_LIMIT]:
        lines.append(f"  changed: {path}")
    for path in removed[:RENDER_LIMIT]:
        lines.append(f"  removed: {path}")
    for entry in failures[:RENDER_LIMIT]:
        lines.append(f"  could not be checked: {entry['path']} ({entry['error']})")
    lines.append(f"  {document['note']}")
    return "\n".join(lines)


def render_diagnostics(document: Mapping[str, Any]) -> str:
    """The C-10 diagnostics summary, grouped by kind so a class of anomaly stands out."""
    require_supported(document)
    rows = document["diagnostics"]
    if not rows:
        return "Diagnostics: none."
    by_kind: dict[str, int] = {}
    for row in rows:
        by_kind[row["kind"]] = by_kind.get(row["kind"], 0) + 1
    summary = ", ".join(f"{kind} {count}" for kind, count in sorted(by_kind.items()))
    lines = [f"Diagnostics: {len(rows)} ({summary})"]
    for row in rows[:RENDER_LIMIT]:
        where = row["path"] or "-"
        if row["line"]:
            where = f"{where}:{row['line']}"
        lines.append(f"  {row['kind']} {where}: {row['message']}")
    lines.extend(_overflow(rows, DIAGNOSTICS_REPORT))
    return "\n".join(lines)


def render_deadcode(document: Mapping[str, Any]) -> str:
    """FR-19's dead-code summary. Carries `DEADCODE_CAVEAT` verbatim (AC-19.2)."""
    require_supported(document)
    groups = document["unreachable"]
    total = sum(len(group["functions"]) for group in groups)
    lines = [f"Dead code: {total} functions unreachable from any detected entry point"]
    if document["no_entry_points_warning"]:
        # AC-18.2/AC-19.3: no entry points means the result is uninformative, not that
        # the codebase is dead.
        lines.append(
            "  Warning: no entry points were detected, so reachability is uninformative "
            "and this list must not be read as dead code."
        )
    for group in groups[:RENDER_LIMIT]:
        names = ", ".join(function["name"] for function in group["functions"][:RENDER_LIMIT])
        lines.append(f"  {group['file']}: {names}")
    lines.extend(_overflow(groups, DEADCODE_REPORT))
    lines.append(f"  {document['caveat']}")
    return "\n".join(lines)


#: The renderer for each §5.3 report, so the run has a single dispatch site.
RENDERERS: dict[str, Callable[[Mapping[str, Any]], str]] = {
    COVERAGE_REPORT: render_coverage,
    EXCLUSIONS_REPORT: render_exclusions,
    REANALYSIS_REPORT: render_reanalysis,
    CHANGE_WARNING_REPORT: render_change_warning,
    DIAGNOSTICS_REPORT: render_diagnostics,
    DEADCODE_REPORT: render_deadcode,
}


def render(filename: str, document: Mapping[str, Any]) -> str:
    """Render the report named `filename` from its parsed document (D9)."""
    try:
        renderer = RENDERERS[filename]
    except KeyError:  # pragma: no cover - a caller bug, not a runtime state
        raise ReportError(f"no renderer for {filename!r}") from None
    return renderer(document)
