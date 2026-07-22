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

**Every call is a full analysis, and that is deliberate.** `changed` is §3.4's incremental
hint; nothing computes one yet (`incremental.py` is task 4.1), so the runner passes `None`
and the index is rebuilt wholesale on every run. A *warm* engine cache would silently break
that: mypy re-type-checks nothing when nothing changed, so `BuildResult.types` comes back
empty and the trees come back absent (measured on 2.3.0; the D6-rule-1 trap in
`mypy_driver`'s docstring), and extracting under those conditions would produce a
complete-looking index that had lost almost every edge. This adapter therefore discards the
engine cache before every build rather than half-using it. The cache each run writes is
left in place, so it is there for the run that learns to reuse it — reuse being the
evict-and-merge discipline D6 makes normative and task 4.1 owns, which is not approximated
here and not partially prepared for.

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


def _resolve_all(
    outcome: BuildOutcome,
    sources: Sequence[EngineSource],
    extractions: dict[str, extract.FileExtraction],
    progress: ProgressSink,
) -> tuple[dict[str, extract.CallResolution], dict[str, externals.FileExternals]]:
    """Resolve every file's call sites, then externalize what left the analyzed set.

    The `TargetIndex` is built once over *every* extracted node, which is what lets a call
    in one file resolve to a definition in another; `externals.resolve()` is told the same
    set of analyzed modules, so a name inside it is never renamed as external (AC-36.4).
    """
    targets = extract.TargetIndex.build(
        sources, [node for one in extractions.values() for node in one.nodes]
    )
    resolutions: dict[str, extract.CallResolution] = {}
    with progress.phase(PHASE_RESOLVE, total=len(sources)):
        for source in sources:
            resolutions[source.relpath] = extract.resolve_calls(
                source, extractions[source.relpath], outcome.types, targets
            )
            progress.advance()
    leaves = externals.resolve(
        [(source, resolutions[source.relpath]) for source in sources],
        [source.module for source in sources],
    )
    return resolutions, leaves


def _fragments(
    outcome: BuildOutcome,
    extractions: dict[str, extract.FileExtraction],
    resolutions: dict[str, extract.CallResolution],
    leaves: dict[str, externals.FileExternals],
    skipped: list[SkipRecord],
) -> list[GraphFragment]:
    """One `GraphFragment` per file whose bytes were read, in path order (§3.4, §4.3).

    An analyzed file's fragment carries everything attributed to it: its own definitions
    and the external leaves it calls, plus its `contains`, `imports` and `calls` edges. A
    skipped file's fragment carries its `files` row and nothing else — which is what makes
    the skip visible to FR-24's hash gate and FR-38's change check on the next run.

    `skipped` is extended in place for any file this run has a hash for but no result of
    either kind, so no read file can leave the adapter unaccounted for.
    """
    reasons = {record.path: record for record in skipped}
    fragments: list[GraphFragment] = []
    for relpath in sorted(outcome.content_hashes):
        digest = outcome.content_hashes[relpath]
        extraction = extractions.get(relpath)
        if extraction is not None:
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
    ) -> AdapterResult:
        """Analyze `files`, returning schema-conformant fragments (design.md §3.4).

        `root` is not read: every path this adapter needs is already on the `SourceFile`s
        (absolute to open, root-relative to name), and the engine's own build roots are
        derived per file from package structure rather than from where the analysis root
        happens to sit (`mypy_driver`'s trap 2). `changed` is accepted per the protocol and
        not consumed: every call re-derives every file it is handed (see the module
        docstring), so there is nothing an incremental hint could narrow yet.

        Raises only `EngineError` — the whole build failing twice, once with a discarded
        cache (design.md §3.5). Every other failure is per-file and comes back as a
        `SkipRecord`.
        """
        _discard_engine_cache(cache_dir)

        outcome = run_build(files, cache_dir, progress)
        sources, extractions, no_tree = _extract_all(outcome, progress)
        skipped = [*outcome.skipped, *no_tree]
        resolutions, leaves = _resolve_all(outcome, sources, extractions, progress)

        diagnostics: list[Diag] = []
        for relpath in sorted(extractions):
            diagnostics.extend(extractions[relpath].diagnostics)
            # `externals` *replaces* the resolution's diagnostics: a site it named is no
            # longer unresolved, so taking both would double-count the C-11 gap.
            diagnostics.extend(leaves[relpath].diagnostics)

        # Built before the result, not inside it: `_fragments` completes `skipped` for any
        # file it finds unaccounted for, and that has to happen before the list is read.
        fragments = _fragments(outcome, extractions, resolutions, leaves, skipped)

        return AdapterResult(
            fragments=fragments,
            skipped=sorted(skipped, key=lambda record: record.path),
            diagnostics=diagnostics,
            # The files whose graph this call re-derived. Every analyzed file, because
            # every call is a full analysis (see the module docstring).
            rechecked={source.path for source in sources},
            engine_meta=dict(outcome.engine_meta),
        )


__all__ = ["PHASE_EXTRACT", "PHASE_RESOLVE", "PythonAdapter"]
