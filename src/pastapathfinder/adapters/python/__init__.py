"""The Python adapter — the only package permitted to import `mypy.*` (AC-23.1).

design.md §1 (the data flow this module walks), §3.4 (the protocol it implements),
§3.5 (the four submodules it composes), D1, D6, D13, D15, D16; requirements FR-6
(AC-6.1, AC-6.2), FR-7, FR-12, FR-13, FR-23, FR-29 (AC-29.1), FR-36, FR-41.

Everything the milestone built separately meets here, and this module adds no analysis of
its own: it drives `mypy_driver` once, walks each analyzed module with `extract`, resolves
that module's call sites through the same module's ladder, hands what left the analyzed set
to `externals`, and assembles the result into one `GraphFragment` per file — the §3.4 shape
the runner validates and stores. The order is design.md §1's, and each step's output is the
next step's input:

1. **build** — one `mypy.build.build()` over the candidates (`mypy_driver.run_build`);
2. **extract** — per analyzed module: the file node, the module-body node, definitions with
   spans, `contains` and `imports` edges (`extract.extract_file`);
3. **resolve** — the call-resolution ladder over every call site the walk found, against a
   `TargetIndex` built from *all* of this run's nodes, so a call into another file resolves
   (`extract.resolve_calls`);
4. **externalize** — D15's two sources turn what left the analyzed set into leaf nodes and
   the edges reaching them (`externals.resolve`).

Steps 3 and 4 need step 2 finished for every file before either can start on any file, which
is why the phases are three passes rather than one loop.

**Full runs re-derive everything; incremental runs re-derive only what mypy re-checked.**
`changed` is §3.4's incremental hint, and it is the switch between the two:

* **`changed is None` — a full run.** The engine cache is discarded first, so mypy re-checks
  every module cold; every file is extracted, resolved and returned. This is what the first
  run of a codebase, and every `--full`, does.
* **`changed` is a set — an incremental run (task 4.1).** The cache is kept, so mypy loads
  the unchanged modules and re-type-checks only the changed files and their dependents — its
  `rechecked_modules` report. Only those files are resolved and returned as fragments; the
  merge (`incremental.merge`) preserves the rest. A warm build re-checks nothing it need not,
  so `BuildResult.types` is populated *only* for the rechecked modules — resolving a
  cache-loaded module would find no types and drop its edges (the D6-rule-1 trap). And a
  cache-loaded module's tree comes back **stripped**: its `defs` are empty (measured on 2.3.0
  even under `preserve_asts=True`), so its structure is no longer walkable either. The nodes a
  re-extracted file must resolve *into* therefore come from `prior_nodes` — the index rows a
  full run once wrote for those files (design.md §3.5's `TargetIndex`; `_target_index`) —
  while the re-extracted files' nodes come fresh from this build.

The re-extracted set is `rechecked_modules` unioned with the changed files themselves: a
changed file that no longer parses is not in `rechecked_modules` (the engine never built it),
yet it must still be re-emitted — as the skip it has become — so the merge evicts its stale
graph. Nothing infers the set from tree presence (the trap that rule 1 forbids).

**What a file costs when something goes wrong.** A per-file failure never stops the run
(FR-6): the pre-flight turns an unparseable or undecodable file into a `SkipRecord` before
the engine sees it (AC-6.1, EC-12), the engine declining a file it was handed is another,
and a file whose bytes could not be read at all is a third. The first two still yield a
`files` row — their bytes were read, so their content hash exists (FR-24's gate reads it
later) — while the third cannot: there is no content to hash, so it leaves a `SkipRecord`
and no fragment, and the runner counts it from that record. When *every* file fails, this
returns fragments and skips with no nodes at all, and the run completes saying so (AC-6.2).
"""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from pathlib import Path

from pastapathfinder.adapters.base import AdapterResult, SourceFile
from pastapathfinder.adapters.python import externals, extract
from pastapathfinder.adapters.python.mypy_driver import BuildOutcome, EngineSource, run_build
from pastapathfinder.discovery import PYTHON_MARKER, PYTHON_SUFFIX, SHEBANG_PREFIX
from pastapathfinder.progress import ProgressSink
from pastapathfinder.schema import (
    LANGUAGE_PYTHON,
    Diag,
    FileRecord,
    GraphFragment,
    NodeRow,
    SkipRecord,
)

#: The countable phases this adapter owns (FR-41/AC-41.1). `mypy_driver` owns the two
#: before them — the pre-flight read and the opaque engine build's heartbeat.
PHASE_EXTRACT = "extracting definitions"
PHASE_RESOLVE = "resolving calls"

#: The skip reason for a file the engine accepted but produced nothing usable for. The
#: pre-flight already proved it parses, so this is an engine outcome, not a source defect.
NO_TREE_DETAIL = "the analysis engine produced no syntax tree for this file"

#: The reason a file gets when it has a content hash but neither an extraction nor a skip
#: record. Nothing in the pipeline produces that state; it is handled rather than assumed
#: away, because the alternative is a discovered file silently absent from the coverage
#: report (FR-7).
UNACCOUNTED_DETAIL = "the analysis engine returned no result for this file"


def _discard_engine_cache(cache_dir: Path) -> None:
    """Remove the engine's incremental cache, so the build re-checks everything.

    See the module docstring: every call here re-derives the whole graph, and a warm cache
    would hand back a build with no types and no trees, from which the extraction below
    would quietly produce an index missing nearly every edge.
    """
    shutil.rmtree(cache_dir, ignore_errors=True)


def _extract_all(
    outcome: BuildOutcome, progress: ProgressSink
) -> tuple[list[EngineSource], dict[str, extract.FileExtraction], list[SkipRecord]]:
    """Walk every analyzed module: `(sources kept, extraction per file, new skips)`.

    A source the engine returned no tree for is dropped here as a per-file engine failure
    (FR-6) rather than extracted as an empty file, which would claim a module with no
    definitions and no calls.
    """
    analyzed_modules = extract.module_index(outcome.sources)
    kept: list[EngineSource] = []
    extractions: dict[str, extract.FileExtraction] = {}
    skipped: list[SkipRecord] = []
    with progress.phase(PHASE_EXTRACT, total=len(outcome.sources)):
        for source in outcome.sources:
            tree = outcome.tree(source.relpath)
            if tree is None:
                skipped.append(
                    SkipRecord(path=source.relpath, reason="engine_error", detail=NO_TREE_DETAIL)
                )
            else:
                kept.append(source)
                extractions[source.relpath] = extract.extract_file(source, tree, analyzed_modules)
            progress.advance()
    return kept, extractions, skipped


def _target_index(
    outcome: BuildOutcome,
    all_sources: Sequence[EngineSource],
    extractions: dict[str, extract.FileExtraction],
    emit: set[str],
    changed: set[Path] | None,
    prior_nodes: Sequence[NodeRow] | None,
) -> extract.TargetIndex:
    """The analyzed set addressable by fullname — this run's rows for what it re-extracted,
    the index's rows for what it did not (design.md §3.5's `TargetIndex`).

    On a full run every module is freshly and fully walked, so the index is built from the
    extractions alone. On an incremental run the engine strips the ASTs of the cache-loaded
    modules it did not re-type-check — their `defs` come back empty (measured on 2.3.0) — so
    walking them yields no definitions; their nodes come from `prior_nodes` (the index rows a
    full run once wrote for them) while the re-extracted files' nodes come fresh. The module
    map spans every analyzed module either way (`outcome.sources`), so a fullname in a
    preserved file still locates its file.
    """
    if changed is None:
        nodes: list[NodeRow] = [node for one in extractions.values() for node in one.nodes]
        return extract.TargetIndex.build(all_sources, nodes)
    fresh = [node for relpath, one in extractions.items() if relpath in emit for node in one.nodes]
    preserved = [
        node
        for node in (prior_nodes or ())
        if node.file_path is not None and node.file_path not in emit
    ]
    return extract.TargetIndex.build(outcome.sources, [*fresh, *preserved])


def _resolve_all(
    outcome: BuildOutcome,
    emit_sources: Sequence[EngineSource],
    extractions: dict[str, extract.FileExtraction],
    targets: extract.TargetIndex,
    progress: ProgressSink,
) -> tuple[dict[str, extract.CallResolution], dict[str, externals.FileExternals]]:
    """Resolve the re-extracted files' call sites against `targets`, then externalize.

    Resolution and externalization need the engine's types, which a warm build populates only
    for the re-extracted set, so they run over `emit_sources` alone. The analyzed-module set
    handed to `externals` is every module (`outcome.sources`), so a name in a preserved file
    is never mistaken for an external one (AC-36.4). On a full run `emit_sources` is every
    source, so this is exactly a full resolution.
    """
    resolutions: dict[str, extract.CallResolution] = {}
    with progress.phase(PHASE_RESOLVE, total=len(emit_sources)):
        for source in emit_sources:
            resolutions[source.relpath] = extract.resolve_calls(
                source, extractions[source.relpath], outcome.types, targets
            )
            progress.advance()
    leaves = externals.resolve(
        [(source, resolutions[source.relpath]) for source in emit_sources],
        [source.module for source in outcome.sources],
    )
    return resolutions, leaves


def _fragments(
    outcome: BuildOutcome,
    extractions: dict[str, extract.FileExtraction],
    resolutions: dict[str, extract.CallResolution],
    leaves: dict[str, externals.FileExternals],
    skipped: list[SkipRecord],
    emit: set[str],
) -> list[GraphFragment]:
    """One `GraphFragment` per re-extracted file whose bytes were read (§3.4, §4.3).

    An analyzed file's fragment carries everything attributed to it: its own definitions
    and the external leaves it calls, plus its `contains`, `imports` and `calls` edges. A
    skipped file's fragment carries its `files` row and nothing else — which is what makes
    the skip visible to FR-24's hash gate and FR-38's change check on the next run.

    `emit` is the re-extracted set — every file on a full run, the rechecked-and-changed set
    on an incremental one — so a file the merge is preserving contributes no fragment and is
    never touched. `skipped` is extended in place for any *emitted* file this run has a hash
    for but no result of either kind, so no re-extracted file leaves the adapter unaccounted
    for. A file resolved for its `TargetIndex` structure but not in `emit` is skipped here.
    """
    reasons = {record.path: record for record in skipped}
    fragments: list[GraphFragment] = []
    for relpath in sorted(emit):
        digest = outcome.content_hashes.get(relpath)
        if digest is None:
            # A re-extracted file whose bytes could not be read: it has no content hash and
            # therefore no `files` row, so it leaves a `SkipRecord` (already in `skipped`)
            # and no fragment. The merge evicts its stale graph from the file set anyway.
            continue
        extraction = extractions.get(relpath)
        if extraction is not None and relpath in resolutions:
            leaf = leaves[relpath]
            fragments.append(
                GraphFragment(
                    file=FileRecord(path=relpath, content_hash=digest, status="analyzed"),
                    nodes=[*extraction.nodes, *leaf.nodes],
                    edges=[*extraction.edges, *resolutions[relpath].edges, *leaf.edges],
                )
            )
            continue
        record = reasons.get(relpath)
        if record is None:  # pragma: no cover - defensive; see UNACCOUNTED_DETAIL
            record = SkipRecord(path=relpath, reason="engine_error", detail=UNACCOUNTED_DETAIL)
            skipped.append(record)
            reasons[relpath] = record
        fragments.append(
            GraphFragment(
                file=FileRecord(
                    path=relpath,
                    content_hash=digest,
                    status="skipped",
                    skip_reason=record.reason,
                )
            )
        )
    return fragments


def _emit_sets(
    outcome: BuildOutcome,
    all_sources: Sequence[EngineSource],
    files: Sequence[SourceFile],
    changed: set[Path] | None,
) -> tuple[set[str], list[EngineSource]]:
    """`(files to emit, sources to resolve)` for this run (see the module docstring).

    On a full run (`changed is None`) both cover everything the run read. On an incremental
    run the sources to resolve are exactly the engine's rechecked set — the only modules whose
    types the warm build populated — and the files to emit add the changed files, so a changed
    file that stopped parsing (never an engine source, so never rechecked) is still re-emitted
    as the skip it became and its stale graph evicted (D6 rule 1).
    """
    if changed is None:
        # Every file handed in, including one whose bytes could not be read (it has no
        # content hash, so `_fragments` emits no fragment for it, but its skip must still be
        # kept and counted — FR-7).
        return {source.relpath for source in files}, list(all_sources)
    changed_relpaths = {source.relpath for source in files if source.path in changed}
    emit_sources = list(outcome.rechecked_sources())
    emit = {source.relpath for source in emit_sources} | changed_relpaths
    return emit, emit_sources


class PythonAdapter:
    """The v1 language adapter (design.md §3.4's protocol; FR-23).

    Stateless: one instance can analyze any number of roots, and nothing it learns in one
    call survives into the next. What persists between runs is the engine cache under
    `cache_dir` and the index itself, both owned by the pipeline.
    """

    language: str = LANGUAGE_PYTHON

    def recognizes(self, path: Path, first_line: bytes | None) -> bool:
        """FR-1's two rules: a `.py` suffix, or an extensionless Python shebang.

        The constants come from `discovery`, which applies the same rules while walking:
        one definition of "a Python source file", so the walk and the adapter cannot
        disagree about what was discovered.
        """
        if path.suffix == PYTHON_SUFFIX:
            return True
        if path.suffix:
            return False
        head = first_line or b""
        return head.startswith(SHEBANG_PREFIX) and PYTHON_MARKER in head

    def analyze(
        self,
        root: Path,
        files: list[SourceFile],
        cache_dir: Path,
        changed: set[Path] | None,
        progress: ProgressSink,
        prior_nodes: Sequence[NodeRow] | None = None,
    ) -> AdapterResult:
        """Analyze `files`, returning schema-conformant fragments (design.md §3.4).

        `root` is not read: every path this adapter needs is already on the `SourceFile`s
        (absolute to open, root-relative to name), and the engine's own build roots are
        derived per file from package structure rather than from where the analysis root
        happens to sit (`mypy_driver`'s trap 2). `changed` is the incremental switch (see the
        module docstring): `None` selects a cold full re-derivation, and a set selects the
        warm build whose re-extracted set is the engine's `rechecked_modules` unioned with the
        changed files. `prior_nodes` seeds the `TargetIndex` for the files that build does not
        re-read, whose cache-loaded ASTs the engine strips (see `_target_index`).

        Raises only `EngineError` — the whole build failing twice, once with a discarded
        cache (design.md §3.5). Every other failure is per-file and comes back as a
        `SkipRecord`.
        """
        if changed is None:
            _discard_engine_cache(cache_dir)

        outcome = run_build(files, cache_dir, progress)
        all_sources, extractions, no_tree = _extract_all(outcome, progress)

        emit, emit_sources = _emit_sets(outcome, all_sources, files, changed)
        # Skips belong to a re-extracted file only: an unchanged file's stale skip stays in
        # the index untouched, so the diagnostics and the merge see this run's skips alone.
        skipped = [record for record in (*outcome.skipped, *no_tree) if record.path in emit]
        targets = _target_index(outcome, all_sources, extractions, emit, changed, prior_nodes)
        resolutions, leaves = _resolve_all(outcome, emit_sources, extractions, targets, progress)

        diagnostics: list[Diag] = []
        for source in emit_sources:
            relpath = source.relpath
            diagnostics.extend(extractions[relpath].diagnostics)
            # `externals` *replaces* the resolution's diagnostics: a site it named is no
            # longer unresolved, so taking both would double-count the C-11 gap.
            diagnostics.extend(leaves[relpath].diagnostics)

        # Built before the result, not inside it: `_fragments` completes `skipped` for any
        # file it finds unaccounted for, and that has to happen before the list is read.
        fragments = _fragments(outcome, extractions, resolutions, leaves, skipped, emit)

        return AdapterResult(
            fragments=fragments,
            skipped=sorted(skipped, key=lambda record: record.path),
            diagnostics=sorted(diagnostics, key=lambda diag: (diag.path or "", diag.line or 0)),
            # The files whose graph this call re-derived — every analyzed file on a full run,
            # the rechecked-and-changed set on an incremental one (D6 rule 1).
            rechecked={source.path for source in files if source.relpath in emit},
            engine_meta=dict(outcome.engine_meta),
            cache_fallback=outcome.cache_fallback,
        )


__all__ = ["PHASE_EXTRACT", "PHASE_RESOLVE", "PythonAdapter"]
