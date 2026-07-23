"""The language-adapter boundary (design.md §3.4, normative; FR-23).

This is the FR-23 seam: source files in, schema-conformant graph fragments out. It is the
only surface through which the pipeline reaches an analysis engine, and it is what makes
AC-23.1 checkable by grep — nothing outside `adapters/python/` may import `mypy.*`, and
nothing in this module knows an engine exists.

`GraphFragment`, `SkipRecord` and `Diag` come from `schema.py` unchanged: the adapter
speaks the index's own row shapes (design.md §4.3), so a fragment needs no translation
between the engine boundary and the store, and FR-21/FR-22 hold by construction.

**The per-file contract.** Every file handed to `analyze()` yields exactly one fragment,
whose `FileRecord` records the content hash of the bytes the adapter actually read and a
status of `analyzed` or `skipped`. A skipped file yields a fragment with that status, its
skip reason, and no nodes — the `files` row is what FR-24's hash gate and FR-38's change
check read later — *plus* one `SkipRecord` carrying the human-readable reason for the
coverage report (AC-7.2). A per-file failure never aborts the run (FR-6).

One case escapes that contract, and only one: a file whose bytes could not be read at all
has no content hash, so it can have no `files` row. It yields a `SkipRecord` alone, and
`runner` counts it from that record — which is why FR-7's arithmetic reconciles over the
union of the two, not over the fragments.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pastapathfinder.progress import ProgressSink
from pastapathfinder.schema import Diag, GraphFragment, NodeRow, SkipRecord


@dataclass(frozen=True, slots=True)
class SourceFile:
    """One analysis input, as handed to an adapter.

    `path` is the absolute real path to open — already deduplicated and symlink-resolved
    by `discovery` — and `relpath` is its root-relative POSIX form, which is the spelling
    every artifact stores (§4.1's `file:` IDs, the `files` table, the reports). Both are
    carried so that no adapter has to re-derive one from the other and risk disagreeing
    with the rest of the run about what a file is called.
    """

    path: Path
    relpath: str


@dataclass(frozen=True, slots=True)
class AdapterResult:
    """What one adapter produced for one run (design.md §3.4).

    * `fragments` — one per file handed in, per the module docstring's contract.
    * `skipped` — the human-readable half of every skipped file (FR-6, AC-7.2).
    * `diagnostics` — non-fatal anomalies for the run's diagnostics report (C-10).
    * `rechecked` — the re-extraction set on an incremental run: the files whose results
      this call re-derived. Consumed by `incremental.merge()` (task 4.1), which per D6
      rule 1 takes it from the engine's own rechecked-modules report and never infers it.
    * `engine_meta` — provenance for the index's `meta` table (`engine`, `engine_version`).
    * `cache_fallback` — True when the engine's incremental cache was found unusable and the
      adapter recovered by wiping it and rebuilding cold (AC-24.3). The runner then publishes
      the run as a fallback and attributes every file `cache_fallback`, rather than the
      incremental attributions a warm build would have earned (FR-35).
    """

    fragments: list[GraphFragment] = field(default_factory=list)
    skipped: list[SkipRecord] = field(default_factory=list)
    diagnostics: list[Diag] = field(default_factory=list)
    rechecked: set[Path] = field(default_factory=set)
    engine_meta: dict[str, Any] = field(default_factory=dict)
    cache_fallback: bool = False


@runtime_checkable
class LanguageAdapter(Protocol):
    """The per-language analyzer interface (design.md §3.4, normative; FR-23)."""

    language: str  # namespace token, e.g. "python" — the §4.1 node-ID namespace

    def recognizes(self, path: Path, first_line: bytes | None) -> bool:
        """Does this adapter claim `path` as one of its source files?

        `first_line` is the head of the file (or None when the caller has not read it),
        so an extensionless file can be claimed on its shebang without every adapter
        re-opening it.
        """
        ...

    def analyze(
        self,
        root: Path,
        files: list[SourceFile],
        cache_dir: Path,
        changed: set[Path] | None,
        progress: ProgressSink,
        prior_nodes: Sequence[NodeRow] | None = None,
    ) -> AdapterResult:
        """Analyze `files` under `root`, returning schema-conformant fragments.

        `cache_dir` is a directory the adapter owns for its engine's incremental cache.
        `changed` is the incremental hint — the files whose content changed since the last
        run, or None on a full run. On a full run the adapter re-derives every file; on an
        incremental one it narrows to the engine's re-extracted set and returns a fragment
        only for each re-derived file (the merge preserves the rest).

        `prior_nodes` is the existing index's nodes, supplied on an incremental run so the
        adapter can resolve a call from a re-extracted file into one it did not re-read.
        design.md §3.5's `TargetIndex` is built from "this run's extractions on a full run,
        or the index's own rows where a file was not re-extracted": an incremental engine
        strips the ASTs of cache-loaded modules, so their structure is no longer walkable
        and must come from the index. `None` on a full run, where every target is freshly
        extracted. `progress` is the run's stderr channel (FR-41).
        """
        ...


def adapter_for(
    adapters: Sequence[LanguageAdapter], path: Path, first_line: bytes | None
) -> LanguageAdapter | None:
    """The first adapter in `adapters` that claims `path`, or None.

    Order is precedence: the list is the pipeline's configured analyzer order, and the
    first claim wins so that one file can never be analyzed twice under two languages.
    """
    for adapter in adapters:
        if adapter.recognizes(path, first_line):
            return adapter
    return None
