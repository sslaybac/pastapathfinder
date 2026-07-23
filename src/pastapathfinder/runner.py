"""Run orchestration: one `analyze` run, end to end (design.md §3.10 `runner`).

design.md §1 (the data flow this module walks in order), §3.10, §3.4 (the adapter it
drives), §3.7 (the detector pass it runs), §3.9 (the reachability it triggers), §5.1
(`--out` derivation), §5.3 (the reports it writes), D9, D17, D18, D19; requirements FR-5,
FR-6, FR-7, FR-8, FR-9, FR-18, FR-19, FR-23, FR-34, FR-41, FR-42, FR-43.

The order is design.md §1's and is not incidental:

1. resolve the root and the configuration, and *prove the output location usable* — a
   run that cannot write its reports should cost nothing (AC-34.2, AC-7.3);
2. compose the exclusion rules and walk the tree (`exclusions`, `discovery`);
3. decide whether this is a full or an incremental run — a compatible prior index and no
   `--full` selects the `incremental` change gate (task 4.1, design.md §3.6);
4. hand the candidates to the language adapter (`adapters.base`), with the incremental
   `changed` set when there is one;
5. write the graph: a first/`--full`/fallback run builds a fresh index atomically
   (`index.full_write`); an incremental run folds the adapter's re-extracted files into the
   existing index (`incremental.merge`). Then, in either case and in the merge order §3.6
   fixes — after the graph is written and (on a merge) orphaned externals are swept — the
   detectors run wholesale over every analyzed file (D18) so entry points are current, and
   reachability (D19) is recomputed and the dead-code findings read out of it;
6. write the six reports, then render each one *from the file just written* (D9);
7. hand back a `RunResult` the CLI turns into an exit code (§3.1).

Step 5's order is the load-bearing part: entry points resolve their targets against the
node IDs the graph now holds, and reachability needs the entry-point nodes the detectors
just emitted. A full run does all of it inside `full_write` (index complete or absent); an
incremental run does all of it inside one transaction against the existing file (index
merged-and-finalized or unchanged) — either way EC-13 holds.

The change gate (§3.6, D18): a run whose candidate hashes and packaging-metadata hash both
match the index re-parses nothing, leaves the index untouched, and reports
`mode: skipped_no_changes` (AC-24.1). A merge that cannot be applied — a fragment fails
validation — wipes the caches and falls back to a full analysis attributing every file
`cache_fallback` (AC-24.3, AC-35.4), announced while it runs (AC-30.2). The post-run change
check (task 4.2) still writes its report with empty lists here, per the C-10 convention.
"""

from __future__ import annotations

import ast
import hashlib
import os
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from pastapathfinder import __version__, incremental, queries, reports
from pastapathfinder.adapters.base import AdapterResult, LanguageAdapter, SourceFile, adapter_for
from pastapathfinder.adapters.python import PythonAdapter
from pastapathfinder.config import Config, load_config
from pastapathfinder.detectors.base import ModuleInput, ProjectInput
from pastapathfinder.detectors.registry import run_detectors
from pastapathfinder.discovery import PROBE_BYTES, DiscoveryResult, discover
from pastapathfinder.exclusions import build_ruleset
from pastapathfinder.index import (
    INDEX_FILENAME,
    Index,
    IndexStoreError,
    full_write,
    open_index,
)
from pastapathfinder.progress import ProgressSink
from pastapathfinder.schema import (
    META_METADATA_HASH,
    Diag,
    FragmentValidationError,
    GraphFragment,
    NodeRow,
    SkipRecord,
)

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
#: finishes, so it runs as an activity phase (AC-41.2), as does reachability — one bounded
#: set of SQL statements with nothing per-file to count.
PHASE_DISCOVERY = "discovering sources"
PHASE_ANALYSIS = "analyzing"
PHASE_DETECT = "detecting entry points"
PHASE_REACHABILITY = "computing reachability"

#: AC-30.2/AC-24.3: the merge-level fallback is announced while it happens, not only in a
#: report. It is distinct from `mypy_driver`'s engine-crash fallback: this one fires when a
#: merge cannot be applied to the existing index, so the recovery is a full rebuild.
MERGE_FALLBACK_NOTICE = (
    "the incremental update could not be applied to the existing index; discarding caches "
    "and running a full analysis — this run will take longer than an incremental one"
)


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


def _source_files(discovered: DiscoveryResult) -> list[SourceFile]:
    """Every candidate as a `SourceFile` (absolute path plus root-relative name).

    The change gate (`incremental.plan_run`) and the adapter both take this shape; building
    it once keeps the two from disagreeing about what a candidate is called.
    """
    return [
        SourceFile(path=path, relpath=discovered.relpath(path)) for path in discovered.candidates
    ]


def _analyze(
    discovered: DiscoveryResult,
    adapters: Sequence[LanguageAdapter],
    cache_dir: Path,
    changed: set[Path] | None,
    progress: ProgressSink,
    prior_nodes: Sequence[NodeRow] | None = None,
) -> AdapterResult:
    """Run every claiming adapter and merge their results into one (design.md §3.4).

    `changed` is the incremental hint threaded to every adapter: `None` on a full run (each
    adapter re-derives everything), or the set of content-changed candidate paths on an
    incremental one (each adapter narrows to its engine's re-extracted set). `prior_nodes` is
    the existing index's nodes, passed on the incremental path so an adapter can resolve into
    a file it did not re-read (design.md §3.5's `TargetIndex`). `cache_fallback` is True if
    any adapter had to discard an unusable cache and rebuild cold (AC-24.3).
    """
    fragments: list[GraphFragment] = []
    skipped: list[SkipRecord] = []
    diagnostics: list[Diag] = []
    rechecked: set[Path] = set()
    engine_meta: dict[str, object] = {}
    cache_fallback = False
    for adapter, files in _assign(discovered, adapters):
        result = adapter.analyze(
            root=discovered.root,
            files=files,
            cache_dir=cache_dir,
            changed=changed,
            progress=progress,
            prior_nodes=prior_nodes,
        )
        fragments.extend(result.fragments)
        skipped.extend(result.skipped)
        diagnostics.extend(result.diagnostics)
        rechecked.update(result.rechecked)
        engine_meta.update(result.engine_meta)
        cache_fallback = cache_fallback or result.cache_fallback
    return AdapterResult(
        fragments=fragments,
        skipped=skipped,
        diagnostics=diagnostics,
        rechecked=rechecked,
        engine_meta=engine_meta,
        cache_fallback=cache_fallback,
    )


# ---------------------------------------------------------------------------
# The detector pass (design.md §3.7, D18)
# ---------------------------------------------------------------------------


def _module_inputs(
    root: Path, analyzed: Sequence[str], node_ids: frozenset[str], progress: ProgressSink
) -> tuple[list[ModuleInput], list[Diag]]:
    """Parse every analyzed file with stdlib `ast` and build the per-module inputs (D14).

    The standard library's parser, not the engine's trees: that is what lets a detector see
    a file the engine produced nothing usable for, and it is why a detector run is a pure
    function of what it is handed (D18).

    A file that cannot be read or parsed here contributes no input. It is not silently
    dropped — an analyzed file the detector pass could not read is exactly the kind of
    non-fatal anomaly the run's diagnostics exist to record (C-10) — and, as with any
    detector failure, the pass continues over every other file (AC-8.2).
    """
    modules: list[ModuleInput] = []
    diagnostics: list[Diag] = []
    with progress.phase(PHASE_DETECT, total=len(analyzed)):
        for relpath in analyzed:
            try:
                tree = ast.parse((root / relpath).read_bytes(), filename=relpath)
            except (OSError, SyntaxError, ValueError) as exc:
                diagnostics.append(
                    Diag(
                        kind="detector_error",
                        path=relpath,
                        message=(
                            f"entry-point detection skipped {relpath}: the file could not be "
                            f"parsed with the standard library parser ({exc})"
                        ),
                    )
                )
            else:
                modules.append(ModuleInput.build(relpath, tree, node_ids))
            progress.advance()
    return modules, diagnostics


def _detect_entry_points(
    store: Index, root: Path, analyzed: Sequence[str], progress: ProgressSink
) -> list[Diag]:
    """Run every detector over the analyzed set and write what they emit (D18, FR-8-11).

    Wholesale, over all analyzed files, on every proceeding run: detectors take no part in
    the incremental evict-and-merge discipline (D18), so this pass is the same work on a
    full run and on an incremental one, and the entry points in the index are always the
    ones the current code declares.

    Targets are resolved against the node IDs the fragments just wrote, which is why this
    runs after `write_fragments()` and not beside it: a declaration naming a function that
    is no longer in the graph must become an unresolved diagnostic (AC-10.2, AC-11.3), and
    that judgement can only be made against a complete graph.
    """
    node_ids = frozenset(store.node_ids())
    modules, diagnostics = _module_inputs(root, analyzed, node_ids, progress)
    output = run_detectors(modules, ProjectInput.discover(root, node_ids))
    store.write_rows(output.nodes, output.edges)
    return [*diagnostics, *output.diagnostics]


def _delete_entry_points(store: Index) -> None:
    """Delete every `entry_point` node and its edges — D18's wholesale recompute, half one.

    Entry points take no part in the incremental evict-and-merge (their edges carry no
    `src_file`, D18); they are cleared and re-emitted from scratch on every proceeding run.
    On a fresh full-write index there is nothing to clear, so this is a no-op there, which is
    what lets the full and incremental paths share one finalize.
    """
    connection = store.connection
    with store.transaction():
        connection.execute(
            "DELETE FROM edges WHERE src IN (SELECT id FROM nodes WHERE kind = 'entry_point')"
        )
        connection.execute("DELETE FROM nodes WHERE kind = 'entry_point'")


def _index_analyzed_files(store: Index) -> list[str]:
    """Every analyzed file's relpath, from the `files` table — the detector pass's input.

    Read from the index rather than the adapter result so the wholesale detector recompute
    (D18) sees *all* analyzed files, not only the ones an incremental run re-extracted.
    """
    return sorted(
        str(row[0])
        for row in store.connection.execute(
            "SELECT path FROM files WHERE status = ?", (reports.STATUS_ANALYZED,)
        )
    )


def _finalize_index(
    store: Index, root: Path, progress: ProgressSink
) -> tuple[list[Diag], queries.DeadCodeResult]:
    """Recompute entry points and reachability over the written graph (§3.6 merge order, D19).

    The load-bearing sequence, shared by the full and incremental paths and run inside their
    respective write scope so it is atomic with the graph write: clear the previous entry
    points, run every detector wholesale over every analyzed file (D18), then recompute
    reachability (D19) and read the dead-code findings out of the freshly marked index. Entry
    points before reachability, because the BFS seeds from `entry_point` nodes.
    """
    analyzed = _index_analyzed_files(store)
    _delete_entry_points(store)
    diagnostics = _detect_entry_points(store, root, analyzed, progress)
    with progress.phase(PHASE_REACHABILITY):
        queries.reachability(store)
        dead = queries.dead_code(store)
    return diagnostics, dead


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _RunOutcome:
    """What a run produced that the reports and the `RunResult` need, path-independent.

    The full, incremental, no-change and fallback paths each build one of these; everything
    downstream — the six documents, the summary, the exit code — reads it without caring
    which path filled it.
    """

    counts: dict[str, int]
    analyzed: list[str]
    skipped: dict[str, str]
    diagnostics: list[Diag]
    dead: queries.DeadCodeResult
    reanalysis_mode: str
    reanalysis_reprocessed: list[tuple[str, str]] = field(default_factory=list)
    reanalysis_removed: list[str] = field(default_factory=list)


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
    `CoverageMismatchError`, `EngineError` — and `cli.main()` maps them all to exit 2 (D10,
    AC-43.3). A run that returns has completed; whether it exits 0 or 1 depends on its skip
    count.

    The run is incremental automatically when a compatible index already exists and `--full`
    was not given (design.md §5.1): `incremental.plan_run` gates on content hashes and the
    packaging-metadata hash, and one of four paths results — a fresh full build, an
    evict-and-merge incremental update, a no-op when nothing changed (AC-24.1), or a
    cache-fallback full rebuild when a merge cannot be applied (AC-24.3).
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
    cache_dir = out_dir / CACHE_DIRNAME
    # AC-5.2: "first analysis of a codebase" is the absence of a prior index, checked
    # before this run writes one.
    first_run = not index_path.exists()

    progress.start_phase(PHASE_DISCOVERY)
    ruleset = build_ruleset(root_path, exclude=config.exclude, reinclude=config.reinclude)
    discovered = discover(root_path, ruleset)
    progress.end_phase()

    base_diagnostics = [*ruleset.diagnostics, *discovered.probe_diagnostics]
    meta_hash = incremental.metadata_hash(discovered.root)

    # `--full`, and the first analysis of a codebase, both take the full path; anything else
    # tries the incremental one, and an index we cannot read is not a usable base for it.
    prior = None if (first_run or full) else _open_prior(index_path)
    if prior is None:
        with progress.phase(PHASE_ANALYSIS, total=len(discovered.candidates)):
            analysis = _analyze(discovered, registered, cache_dir, None, progress)
        outcome = _build_full_index(
            analysis,
            discovered,
            index_path,
            run,
            meta_hash,
            progress,
            base_diagnostics=base_diagnostics,
            mode=reports.MODE_FULL,
        )
    else:
        try:
            outcome = _run_incremental(
                prior,
                discovered,
                registered,
                index_path,
                cache_dir,
                run,
                meta_hash,
                progress,
                base_diagnostics,
            )
        finally:
            prior.close()

    run = run.finish(time.monotonic() - started)
    documents = _documents(run, discovered, outcome)
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
        counts=outcome.counts,
        run=run,
        diagnostics=tuple(outcome.diagnostics),
        report_paths=report_paths,
    )


def _open_prior(index_path: Path) -> Index | None:
    """Open the existing index for an incremental base, or None if it is unusable.

    An index that is absent, unreadable, or a schema version this build does not support is
    not a base an incremental run can merge into (AC-39.2/39.3); `analyze` rebuilds it from
    scratch on the full path rather than failing, which is what "re-run analyze" would do
    anyway. It is opened writable, because a merge updates it in place (design.md §3.8).
    """
    try:
        return open_index(index_path, read_only=False)
    except IndexStoreError:
        return None


def _prior_nodes(index: Index) -> list[NodeRow]:
    """Every node the index already holds — the `TargetIndex` seed for preserved files.

    Only the columns the resolution ladder reads are needed (id, kind, file_path, span,
    is_external), so the rows are cheap to carry even on a large index; the adapter keeps the
    ones whose file it did not re-extract and discards the rest.
    """
    return [
        NodeRow(
            id=str(row[0]),
            kind=str(row[1]),
            name=str(row[2]),
            language=str(row[3]),
            file_path=None if row[4] is None else str(row[4]),
            start_line=None if row[5] is None else int(row[5]),
            end_line=None if row[6] is None else int(row[6]),
            is_external=int(row[7]),
        )
        for row in index.connection.execute(
            "SELECT id, kind, name, language, file_path, start_line, end_line, is_external"
            " FROM nodes"
        )
    ]


def _meta(
    analysis: AdapterResult, discovered: DiscoveryResult, run: reports.RunInfo, meta_hash: str
) -> dict[str, str]:
    """The index `meta` a full write stamps (design.md §4.2), including D18's metadata hash."""
    return {
        "tool_version": __version__,
        # Whatever the adapter reports having used, and "none" when no adapter ran at all
        # (a tree with no recognized sources): the index names an engine it was analyzed
        # with, never one that was merely available.
        "engine": str(analysis.engine_meta.get("engine", "none")),
        "engine_version": str(analysis.engine_meta.get("engine_version", "none")),
        "root_path": str(discovered.root),
        "created_at": run.started_at,
        "run_id": run.run_id,
        META_METADATA_HASH: meta_hash,
    }


def _build_full_index(
    analysis: AdapterResult,
    discovered: DiscoveryResult,
    index_path: Path,
    run: reports.RunInfo,
    meta_hash: str,
    progress: ProgressSink,
    *,
    base_diagnostics: Sequence[Diag],
    mode: str,
) -> _RunOutcome:
    """Write a complete index atomically from a full adapter result (design.md §3.8, §1).

    The first run, every `--full`, and the cache-fallback recovery all land here: the whole
    graph is written to a staging file and renamed into place, and inside that same write the
    finalize sequence recomputes entry points and reachability. Coverage is reconciled from
    the adapter's own result (every file it processed) before anything is published (AC-7.1).
    A fallback attributes every re-processed file `cache_fallback`; a plain full run leaves
    the incremental `reprocessed` list empty, since `mode: full` is the whole statement.
    """
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
    reports.assert_coverage_reconciles(counts)
    diagnostics = [*base_diagnostics, *analysis.diagnostics]

    with full_write(index_path, _meta(analysis, discovered, run, meta_hash)) as store:
        store.write_fragments(analysis.fragments)
        det_diags, dead = _finalize_index(store, discovered.root, progress)
    diagnostics.extend(det_diags)

    if mode == reports.MODE_FALLBACK:
        reprocessed = [
            (path, reports.REASON_CACHE_FALLBACK) for path in sorted([*analyzed, *skipped])
        ]
    else:
        reprocessed = []
    return _RunOutcome(
        counts=counts,
        analyzed=analyzed,
        skipped=skipped,
        diagnostics=diagnostics,
        dead=dead,
        reanalysis_mode=mode,
        reanalysis_reprocessed=reprocessed,
    )


def _run_incremental(
    prior: Index,
    discovered: DiscoveryResult,
    registered: Sequence[LanguageAdapter],
    index_path: Path,
    cache_dir: Path,
    run: reports.RunInfo,
    meta_hash: str,
    progress: ProgressSink,
    base_diagnostics: Sequence[Diag],
) -> _RunOutcome:
    """The incremental path: gate, then merge — or skip, or fall back (design.md §3.6).

    `plan_run` hashes every candidate and the packaging metadata. Nothing changed → the
    index is left untouched (AC-24.1). Otherwise the adapter re-derives only the changed
    files and their dependents, and `merge` folds them in, followed by the wholesale
    entry-point and reachability recompute — all in one transaction, so the index is either
    merged-and-finalized or unchanged (EC-13). A merge that fails validation, or an adapter
    that had to rebuild cold, drops to a full analysis attributed `cache_fallback` (AC-24.3).
    """
    sources = _source_files(discovered)
    plan = incremental.plan_run(prior, sources)

    if not plan.has_changes:
        # AC-24.1/35.2: nothing re-parsed, index untouched. The reports are still written,
        # their dead-code and coverage read from the unchanged index.
        analyzed, skipped, counts = _coverage_from_index(prior, discovered, {})
        return _RunOutcome(
            counts=counts,
            analyzed=analyzed,
            skipped=skipped,
            diagnostics=list(base_diagnostics),
            dead=queries.dead_code(prior),
            reanalysis_mode=reports.MODE_SKIPPED_NO_CHANGES,
        )

    if plan.needs_engine:
        changed_abs = {source.path for source in sources if source.relpath in plan.changed}
        # The adapter resolves a re-extracted file's calls into files it did not re-read
        # against the index's existing nodes (design.md §3.5): a warm build strips the ASTs
        # of cache-loaded modules, so their structure is no longer walkable.
        prior_nodes = _prior_nodes(prior)
        with progress.phase(PHASE_ANALYSIS, total=len(sources)):
            analysis = _analyze(
                discovered, registered, cache_dir, changed_abs, progress, prior_nodes
            )
    else:
        # Metadata-only change (D18): no Python source moved, so no engine pass — but the run
        # still proceeds so the wholesale detector recompute reads the new packaging metadata.
        analysis = AdapterResult()

    if analysis.cache_fallback:
        # The adapter's warm build was unusable and it rebuilt cold; its result is already
        # complete, so publish it as a full fallback rather than merging (AC-24.3, AC-30.2).
        progress.note(MERGE_FALLBACK_NOTICE)
        prior.close()
        return _build_full_index(
            analysis,
            discovered,
            index_path,
            run,
            meta_hash,
            progress,
            base_diagnostics=base_diagnostics,
            mode=reports.MODE_FALLBACK,
        )

    merge_failed = False
    try:
        with prior.transaction():
            try:
                merge_report = incremental.merge(prior, analysis, plan)
            except FragmentValidationError:
                # A fragment the merge cannot apply: the incremental input is corrupt. Roll
                # the transaction back (index unchanged) and fall back below. A *finalize*
                # validation failure (a detector-ID collision, §8-O7) is a different, unfixed
                # defect and must surface as the run failure it is — hence the flag.
                merge_failed = True
                raise
            det_diags, dead = _finalize_index(prior, discovered.root, progress)
    except FragmentValidationError:
        if not merge_failed:
            raise
        return _fallback_full(
            discovered,
            registered,
            index_path,
            cache_dir,
            run,
            meta_hash,
            progress,
            base_diagnostics,
        )

    run_skips = _skip_reasons(analysis)
    analyzed, skipped, counts = _coverage_from_index(prior, discovered, run_skips)
    diagnostics = [*base_diagnostics, *analysis.diagnostics, *det_diags]
    return _RunOutcome(
        counts=counts,
        analyzed=analyzed,
        skipped=skipped,
        diagnostics=diagnostics,
        dead=dead,
        reanalysis_mode=merge_report.mode,
        reanalysis_reprocessed=list(merge_report.reprocessed),
        reanalysis_removed=list(merge_report.removed),
    )


def _fallback_full(
    discovered: DiscoveryResult,
    registered: Sequence[LanguageAdapter],
    index_path: Path,
    cache_dir: Path,
    run: reports.RunInfo,
    meta_hash: str,
    progress: ProgressSink,
    base_diagnostics: Sequence[Diag],
) -> _RunOutcome:
    """Recover from a failed merge with a wipe-and-rebuild full analysis (AC-24.3/35.4/30.2).

    Announced while it runs, then a full analysis with `changed=None` — which discards the
    engine cache and rebuilds cold — published as a fresh index with every file attributed
    `cache_fallback`. The prior index the caller could not merge into is left behind and
    atomically overwritten by the full write.
    """
    progress.note(MERGE_FALLBACK_NOTICE)
    with progress.phase(PHASE_ANALYSIS, total=len(discovered.candidates)):
        analysis = _analyze(discovered, registered, cache_dir, None, progress)
    return _build_full_index(
        analysis,
        discovered,
        index_path,
        run,
        meta_hash,
        progress,
        base_diagnostics=base_diagnostics,
        mode=reports.MODE_FALLBACK,
    )


def _coverage_from_index(
    store: Index, discovered: DiscoveryResult, run_skips: Mapping[str, str]
) -> tuple[list[str], dict[str, str], dict[str, int]]:
    """Coverage over the whole codebase, read from the merged index (FR-7, D17).

    An incremental run re-derives only some files, so coverage cannot come from the adapter
    result the way a full run's does; it comes from the `files` table, which after the merge
    describes the whole current codebase. `run_skips` supplies this run's human-readable skip
    reasons (AC-7.2) — richer than the reason class the index stores — and adds any file that
    could not be read at all, which has no `files` row. The reconciliation holds by
    construction, since discovered is defined as the three parts summed.
    """
    analyzed: list[str] = []
    skipped: dict[str, str] = {}
    for path, status, skip_reason in store.connection.execute(
        "SELECT path, status, skip_reason FROM files"
    ):
        if status == reports.STATUS_ANALYZED:
            analyzed.append(str(path))
        else:
            skipped[str(path)] = str(skip_reason) if skip_reason else "not analyzed"
    skipped.update(run_skips)

    excluded = len(discovered.excluded)
    counts = reports.coverage_counts(
        discovered=len(analyzed) + len(skipped) + excluded,
        analyzed=len(analyzed),
        skipped=len(skipped),
        excluded=excluded,
    )
    reports.assert_coverage_reconciles(counts)
    return sorted(analyzed), skipped, counts


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
    run: reports.RunInfo, discovered: DiscoveryResult, outcome: _RunOutcome
) -> dict[str, dict[str, object]]:
    """Build all six §5.3 documents. The coverage document asserts AC-7.1 as it is built."""
    rows = [reports.analyzed_row(path) for path in outcome.analyzed]
    rows += [reports.skipped_row(path, reason) for path, reason in outcome.skipped.items()]
    rows += [reports.excluded_row(record) for record in discovered.excluded]

    return {
        reports.COVERAGE_REPORT: reports.coverage_document(run, outcome.counts, rows),
        reports.EXCLUSIONS_REPORT: reports.exclusions_document(run, discovered.excluded),
        reports.REANALYSIS_REPORT: reports.reanalysis_document(
            run,
            mode=outcome.reanalysis_mode,
            reprocessed=[
                {"path": path, "reason": reason} for path, reason in outcome.reanalysis_reprocessed
            ],
            removed=outcome.reanalysis_removed,
        ),
        reports.DIAGNOSTICS_REPORT: reports.diagnostics_document(run, outcome.diagnostics),
        # AC-19.3: the warning travels with the findings, so a run with no entry points
        # cannot present a codebase-wide list as though it meant something.
        reports.DEADCODE_REPORT: reports.deadcode_document(
            run,
            no_entry_points_warning=outcome.dead.no_entry_points_warning,
            unreachable=[group.as_json() for group in outcome.dead.unreachable],
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
