"""Run orchestration: one `analyze` run, end to end (design.md §3.10 `runner`).

design.md §1 (the data flow this module walks in order), §3.10, §3.4 (the adapter it
drives), §5.1 (`--out` derivation), §5.3 (the reports it writes), D9, D17; requirements
FR-5, FR-6, FR-7, FR-23, FR-34, FR-41, FR-42, FR-43.

The order is design.md §1's and is not incidental:

1. resolve the root and the configuration, and *prove the output location usable* — a
   run that cannot write its reports should cost nothing (AC-34.2, AC-7.3);
2. compose the exclusion rules and walk the tree (`exclusions`, `discovery`);
3. hand the surviving candidates to the language adapter (`adapters.base`);
4. write the index atomically (`index.full_write`);
5. write the six reports, then render each one *from the file just written* (D9);
6. hand back a `RunResult` the CLI turns into an exit code (§3.1).

Steps this task does not own are visible in the shape but not built here: incremental
planning and the re-analysis report's content (task 4.1), entry-point detection (3.1-3.3),
reachability and dead code (3.4), and the post-run change check (4.2) all arrive later.
Their reports are written now with empty lists, per the C-10 convention that an artifact
is always produced even when it has nothing to say.
"""

from __future__ import annotations

import hashlib
import os
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from pastapathfinder import __version__, reports
from pastapathfinder.adapters.base import AdapterResult, LanguageAdapter, SourceFile, adapter_for
from pastapathfinder.adapters.python import PythonAdapter
from pastapathfinder.config import Config, load_config
from pastapathfinder.discovery import PROBE_BYTES, DiscoveryResult, discover
from pastapathfinder.exclusions import build_ruleset
from pastapathfinder.index import INDEX_FILENAME, full_write
from pastapathfinder.progress import ProgressSink
from pastapathfinder.schema import Diag, GraphFragment, SkipRecord

#: The engine cache directory handed to the adapter, under the run's output directory.
#: design.md §3.5 pins mypy's `cache_dir` to `<out>/mypy_cache`; the name is spelled here
#: rather than in the adapter so the pipeline stays in charge of the output tree's layout.
#: It is a path, not an engine API — nothing in this module imports or knows the engine
#: (AC-23.1).
CACHE_DIRNAME = "mypy_cache"

#: §5.1's derived output location lives under the XDG data directory, deliberately
#: outside the analyzed tree: the tool never writes into the codebase and never discovers
#: its own output.
XDG_DATA_HOME = "XDG_DATA_HOME"
DATA_SUBDIR = "pastapathfinder"

#: Length of the path digest in the derived directory name (§5.1).
DIGEST_LENGTH = 12

#: The progress phases of a run (FR-41). Discovery's total is unknowable before the walk
#: finishes, so it runs as an activity phase (AC-41.2).
PHASE_DISCOVERY = "discovering sources"
PHASE_ANALYSIS = "analyzing"


class RunnerError(Exception):
    """A run cannot proceed. `cli.main()` maps it to exit 2 with the message (D10)."""


class OutputLocationError(RunnerError):
    """The output directory cannot be created or written (AC-34.2, FR-34).

    Names the path and the reason, and never suggests elevation: FR-34 requires the tool
    to work unprivileged, so asking for root would be advice against its own design.
    """


@dataclass(frozen=True, slots=True)
class RunResult:
    """What a completed run produced; the CLI's input for the exit code (§3.1)."""

    completed: bool
    root: Path
    out_dir: Path
    index_path: Path
    reports_dir: Path
    counts: dict[str, int]
    run: reports.RunInfo
    diagnostics: tuple[Diag, ...] = ()
    report_paths: dict[str, Path] = field(default_factory=dict)

    @property
    def files_analyzed(self) -> int:
        return self.counts[reports.COUNT_ANALYZED]

    @property
    def files_skipped(self) -> int:
        return self.counts[reports.COUNT_SKIPPED]


def default_adapters() -> tuple[LanguageAdapter, ...]:
    """The language adapters this build registers, in precedence order (FR-23).

    One, because v1 analyzes one language (requirements §6 item 6). The tuple is the
    whole of the pipeline's knowledge that an engine exists: `adapters.python` is the only
    package that imports one (AC-23.1), and nothing here or downstream refers to mypy by
    name — an index's `meta.engine` is whatever the adapter reports having used.
    """
    return (PythonAdapter(),)


# ---------------------------------------------------------------------------
# Output location (design.md §5.1)
# ---------------------------------------------------------------------------


def derive_out_dir(root: Path) -> Path:
    """`$XDG_DATA_HOME/pastapathfinder/<basename>-<sha256(abspath)[:12]>/` (§5.1).

    The digest keeps two same-named roots apart; the basename keeps the directory
    recognizable to a human browsing it.
    """
    base = os.environ.get(XDG_DATA_HOME)
    home = Path(base) if base and base.startswith("/") else Path.home() / ".local" / "share"
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:DIGEST_LENGTH]
    return home / DATA_SUBDIR / f"{root.name or 'root'}-{digest}"


def resolve_out_dir(root: Path, requested: Path | str | None, config: Config) -> Path:
    """`--out`, else the config's `[output] dir`, else §5.1's derived location."""
    if requested is not None:
        return Path(requested).expanduser().resolve()
    if config.out_dir is not None:
        return Path(config.out_dir).expanduser().resolve()
    return derive_out_dir(root)


def prepare_out_dir(out_dir: Path) -> Path:
    """Create the output directory, failing the run by name if it cannot be (AC-34.2)."""
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OutputLocationError(
            f"cannot create the output directory {out_dir}: {exc.strerror or exc}; "
            f"choose a writable location with --out or [output] dir"
        ) from exc
    if not os.access(out_dir, os.W_OK):
        raise OutputLocationError(
            f"the output directory {out_dir} is not writable; "
            f"choose a writable location with --out or [output] dir"
        )
    return out_dir


# ---------------------------------------------------------------------------
# The adapter step (design.md §3.4)
# ---------------------------------------------------------------------------


def _first_line(path: Path) -> bytes | None:
    """The head of `path`, for `recognizes()` — read only for extensionless files."""
    try:
        with path.open("rb") as stream:
            return stream.read(PROBE_BYTES).split(b"\n", 1)[0]
    except OSError:
        return None


def _assign(
    discovered: DiscoveryResult, adapters: Sequence[LanguageAdapter]
) -> list[tuple[LanguageAdapter, list[SourceFile]]]:
    """Group the candidates by the adapter that claims each, preserving order."""
    assignments: dict[str, tuple[LanguageAdapter, list[SourceFile]]] = {}
    for path in discovered.candidates:
        head = _first_line(path) if path.suffix == "" else None
        adapter = adapter_for(adapters, path, head)
        if adapter is None:
            raise RunnerError(
                f"no language adapter recognizes {discovered.relpath(path)}"
                + (
                    "; this build registers no language adapters"
                    if not adapters
                    else f"; registered: {[a.language for a in adapters]}"
                )
            )
        _, files = assignments.setdefault(adapter.language, (adapter, []))
        files.append(SourceFile(path=path, relpath=discovered.relpath(path)))
    return list(assignments.values())


def _analyze(
    discovered: DiscoveryResult,
    adapters: Sequence[LanguageAdapter],
    cache_dir: Path,
    progress: ProgressSink,
) -> AdapterResult:
    """Run every claiming adapter and merge their results into one (design.md §3.4)."""
    fragments: list[GraphFragment] = []
    skipped: list[SkipRecord] = []
    diagnostics: list[Diag] = []
    rechecked: set[Path] = set()
    engine_meta: dict[str, object] = {}
    for adapter, files in _assign(discovered, adapters):
        result = adapter.analyze(
            root=discovered.root,
            files=files,
            cache_dir=cache_dir,
            changed=None,  # incremental planning arrives in task 4.1
            progress=progress,
        )
        fragments.extend(result.fragments)
        skipped.extend(result.skipped)
        diagnostics.extend(result.diagnostics)
        rechecked.update(result.rechecked)
        engine_meta.update(result.engine_meta)
    return AdapterResult(
        fragments=fragments,
        skipped=skipped,
        diagnostics=diagnostics,
        rechecked=rechecked,
        engine_meta=engine_meta,
    )


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def run_analysis(
    root: Path | str,
    *,
    out: Path | str | None = None,
    config_path: Path | str | None = None,
    full: bool = False,
    adapters: Sequence[LanguageAdapter] | None = None,
    progress: ProgressSink | None = None,
    stdout: TextIO | None = None,
) -> RunResult:
    """Analyze the codebase at `root`, writing the index and the six §5.3 reports.

    Every failure that prevents the run from completing raises — `RootError`,
    `ConfigError`, `InvalidPatternError`, `OutputLocationError`, `ReportWriteError`,
    `CoverageMismatchError` — and `cli.main()` maps them all to exit 2 (D10, AC-43.3). A
    run that returns has completed; whether it exits 0 or 1 depends on its skip count.

    `full` is accepted for the §5.1 surface. Until task 4.1 builds the incremental path,
    every run is a full run, so the flag currently selects the behavior that is already
    the only one; the re-analysis report says `mode: full` accordingly.
    """
    started = time.monotonic()
    run = reports.RunInfo.start()
    progress = progress if progress is not None else ProgressSink()
    stream = stdout if stdout is not None else sys.stdout
    registered = tuple(default_adapters() if adapters is None else adapters)

    root_path = Path(root).expanduser().resolve()
    config = load_config(root_path, Path(config_path) if config_path is not None else None)

    out_dir = prepare_out_dir(resolve_out_dir(root_path, out, config))
    reports_dir = reports.prepare_report_dir(out_dir)
    index_path = out_dir / INDEX_FILENAME
    # AC-5.2: "first analysis of a codebase" is the absence of a prior index, checked
    # before this run writes one.
    first_run = not index_path.exists()

    progress.start_phase(PHASE_DISCOVERY)
    ruleset = build_ruleset(root_path, exclude=config.exclude, reinclude=config.reinclude)
    discovered = discover(root_path, ruleset)
    progress.end_phase()

    with progress.phase(PHASE_ANALYSIS, total=len(discovered.candidates)):
        analysis = _analyze(discovered, registered, out_dir / CACHE_DIRNAME, progress)

    analyzed = [
        fragment.file.path
        for fragment in analysis.fragments
        if fragment.file.status == reports.STATUS_ANALYZED
    ]
    skipped = _skip_reasons(analysis)

    counts = reports.coverage_counts(
        discovered=len(discovered.candidates) + len(discovered.excluded),
        analyzed=len(analyzed),
        skipped=len(skipped),
        excluded=len(discovered.excluded),
    )
    # AC-7.1, checked before anything is published: what discovery found and what the
    # adapter returned must account for each other. They are computed from independent
    # sources, so a disagreement is a real defect — and a run that cannot say what it
    # covered must not leave an index behind claiming it covered something.
    reports.assert_coverage_reconciles(counts)
    diagnostics = [*ruleset.diagnostics, *discovered.probe_diagnostics, *analysis.diagnostics]

    meta = {
        "tool_version": __version__,
        # Whatever the adapter reports having used, and "none" when no adapter ran at all
        # (a tree with no recognized sources): the index names an engine it was analyzed
        # with, never one that was merely available.
        "engine": str(analysis.engine_meta.get("engine", "none")),
        "engine_version": str(analysis.engine_meta.get("engine_version", "none")),
        "root_path": str(discovered.root),
        "created_at": run.started_at,
        "run_id": run.run_id,
    }
    with full_write(index_path, meta) as store:
        store.write_fragments(analysis.fragments)

    run = run.finish(time.monotonic() - started)
    documents = _documents(run, discovered, analysis, counts, analyzed, skipped, diagnostics)
    report_paths = {
        filename: reports.write_report(reports_dir, filename, document)
        for filename, document in documents.items()
    }
    _print_summary(stream, report_paths, first_run=first_run)
    print(f"Index: {index_path}", file=stream)
    print(f"Reports: {reports_dir}", file=stream)

    return RunResult(
        completed=True,
        root=discovered.root,
        out_dir=out_dir,
        index_path=index_path,
        reports_dir=reports_dir,
        counts=counts,
        run=run,
        diagnostics=tuple(diagnostics),
        report_paths=report_paths,
    )


def _skip_reasons(analysis: AdapterResult) -> dict[str, str]:
    """`{path: human-readable reason}` for every file the adapter did not analyze (AC-7.2).

    Two sources that normally agree file for file: the fragment a skipped file still
    contributes (its `files` row, carrying the schema's reason class) and the `SkipRecord`
    carrying the words a human reads. They diverge in exactly one direction — a file whose
    bytes could not be read has no content hash and therefore no `files` row, so it arrives
    as a record alone (design.md §3.4) — and taking the union is what keeps such a file in
    the coverage report instead of vanishing from FR-7's arithmetic.
    """
    reasons = {
        fragment.file.path: fragment.file.skip_reason or "not analyzed"
        for fragment in analysis.fragments
        if fragment.file.status == reports.STATUS_SKIPPED
    }
    for record in analysis.skipped:
        reasons[record.path] = record.detail or record.reason
    return reasons


def _documents(
    run: reports.RunInfo,
    discovered: DiscoveryResult,
    analysis: AdapterResult,
    counts: dict[str, int],
    analyzed: Sequence[str],
    skipped: Mapping[str, str],
    diagnostics: Sequence[Diag],
) -> dict[str, dict[str, object]]:
    """Build all six §5.3 documents. The coverage document asserts AC-7.1 as it is built."""
    rows = [reports.analyzed_row(path) for path in analyzed]
    rows += [reports.skipped_row(path, reason) for path, reason in skipped.items()]
    rows += [reports.excluded_row(record) for record in discovered.excluded]

    # No detector has run yet (tasks 3.1-3.3), so a run's entry points are whatever the
    # adapter emitted — none in practice. The warning states the fact rather than
    # asserting a reachability result this task does not compute.
    has_entry_points = any(
        node.kind == "entry_point" for fragment in analysis.fragments for node in fragment.nodes
    )
    return {
        reports.COVERAGE_REPORT: reports.coverage_document(run, counts, rows),
        reports.EXCLUSIONS_REPORT: reports.exclusions_document(run, discovered.excluded),
        reports.REANALYSIS_REPORT: reports.reanalysis_document(run, mode=reports.MODE_FULL),
        reports.DIAGNOSTICS_REPORT: reports.diagnostics_document(run, diagnostics),
        reports.DEADCODE_REPORT: reports.deadcode_document(
            run, no_entry_points_warning=not has_entry_points
        ),
        reports.CHANGE_WARNING_REPORT: reports.change_warning_document(run),
    }


def _print_summary(stream: TextIO, report_paths: dict[str, Path], *, first_run: bool) -> None:
    """Render each report from the file just written (D9), in §5.3's order.

    Reading the file back is the point: the structured form is authoritative by
    construction, so a rendering can never claim something the artifact does not say.
    """
    for filename in reports.REPORT_FILENAMES:
        path = report_paths.get(filename)
        if path is None:  # pragma: no cover - every run writes every report
            continue
        text = reports.render(filename, reports.load_report(path))
        if text:
            print(text, file=stream)
        if filename == reports.EXCLUSIONS_REPORT and first_run:
            # AC-5.2: on the first analysis of a codebase the exclusion report is put in
            # front of the user, because EC-8's audit trail is worthless unread.
            print(f"  First run of this codebase — full exclusion report: {path}", file=stream)
