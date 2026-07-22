"""The engine boundary: the only module that calls mypy (design.md §3.5, D1, D1a, D13).

design.md §3.5 (`mypy_driver`, normative settings and the enumerated internals),
§3.10 (`progress` — the heartbeat this module drives), D1, D1a, D13;
requirements FR-6 (AC-6.1, AC-6.2), FR-13 (AC-13.1, AC-13.2), FR-24 (AC-24.3),
FR-30 (AC-30.2), FR-41 (AC-41.2), EC-12.

Everything mypy-shaped stops here. The module hands the engine a list of files and hands
back four things and nothing else: the typed module graph, the expression→type map, the
set of modules the engine actually re-type-checked, and the per-file failures expressed
as `SkipRecord`s. Extraction (`extract.py`, task 2.2) reads those; it never calls mypy.
The enumerated internals of design.md §3.5 — the D1a upgrade checklist — are exactly the
surface used below: `mypy.build.build`, `BuildSource`, `Options`, `BuildResult.graph` /
`.types`, `State.tree`, and the build manager's rechecked-modules report.

**Three traps this module exists to have already paid for** (`FINDINGS-mypy.md` §2, and
`FINDINGS-session5.md` Part 1). Each is silent — none produces an error — so each is
reproduced by a test rather than trusted to a comment:

1. **`mypy_path` must carry the build root**, or `from sibling import f` resolves to a
   `Var: Any` with no `.node` and recall collapses without a single diagnostic. The build
   root is *not* the analysis root: it is the directory a module is importable from,
   which for the pinned Django benchmark (`django/`, a package) is that directory's
   *parent*. `_crawl_up()` derives it per file, and every distinct root goes on
   `mypy_path`.
2. **File root and build root are different things.** Paths are enumerated relative to
   the analysis root (and stay that way in every artifact); module names are relative to
   the build root. Joining one against the other produced `CompileError: Cannot read
   file …` in the prototype.
3. **A warm build with nothing changed reloads zero trees.** `State.tree` is present for
   cache-loaded modules that were never re-type-checked, and `BuildResult.types` is empty
   for them — so inferring the re-extraction set from tree presence silently drops those
   files' cached edges (the prototype's spurious 8,383-edge gap). The re-extraction set
   is `manager.rechecked_modules` and nothing else; `BuildOutcome.rechecked_modules` is
   that report, unmodified (D6 rule 1).

**Why files are parsed twice.** Every candidate is decoded and parsed with the stdlib
`ast` module *before* the engine sees it. mypy treats a syntax error as a build blocker
and raises `CompileError` for the whole run — measured on 2.3.0 — which would fail the
run over one bad file and defeat FR-6. Pre-flighting turns that into a per-file
`SkipRecord` (`parse_error` / `encoding_error`, EC-1/EC-2/EC-12) and keeps the offender
out of the build. It costs one parse pass (~1.9 s over Django's 908 files) and it is what
makes AC-6.1 and AC-6.2 true. The host interpreter's grammar is mypy's grammar (D2), so
the pre-flight accepts what the engine would.
"""

from __future__ import annotations

import ast
import hashlib
import io
import shutil
import tokenize
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import mypy.version
from mypy.build import BuildResult, State
from mypy.build import build as _build
from mypy.modulefinder import BuildSource
from mypy.nodes import MypyFile
from mypy.options import Options

from pastapathfinder.adapters.base import SourceFile
from pastapathfinder.progress import ProgressSink
from pastapathfinder.schema import SkipRecord

# ---------------------------------------------------------------------------
# Engine identity (design.md §4.2 `meta`, D1a)
# ---------------------------------------------------------------------------

#: The `meta.engine` value this adapter writes.
ENGINE_NAME = "mypy"

#: D1's exact pin, mirrored from `pyproject.toml`. Nothing at run time refuses a
#: different version — the pin is the packaging's job — but the D1a revalidation
#: procedure starts from a test asserting the installed engine is this one.
PINNED_ENGINE_VERSION = "2.3.0"

#: The engine actually loaded, written to `meta.engine_version`.
ENGINE_VERSION = mypy.version.__version__

# ---------------------------------------------------------------------------
# Progress phases (design.md §3.10; FR-41)
# ---------------------------------------------------------------------------

#: The countable pre-flight: read, decode, parse (AC-41.1).
PHASE_READ = "reading sources"

#: design.md §3.10 makes this heartbeat normative: `mypy.build.build()` is one opaque
#: call that can run for minutes, and silence there is indistinguishable from a hang.
#: The sink renders it as `analyzing (engine build) … {elapsed}s` (AC-41.2).
PHASE_BUILD = "analyzing (engine build)"

#: AC-30.2/AC-24.3: the fallback is announced while it happens, not only in a report.
FALLBACK_NOTICE = (
    "engine build failed; discarding the engine cache and running a full rebuild — "
    "this run will take longer than an incremental one"
)


class EngineError(Exception):
    """The engine failed the whole build twice, the second time with a clean cache.

    Run-terminating: there is no graph to report on. `cli.main()` maps it to exit 2 with
    the message (D10, AC-43.3), so the message carries the engine's own error text —
    design.md §3.5 requires the engine error to be surfaced, not summarized away.
    """


# ---------------------------------------------------------------------------
# What the engine is given, and what it gives back
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EngineSource:
    """One file the engine was asked to analyze.

    `relpath` is the analysis-root-relative POSIX name every artifact stores; `module`
    and `source_root` are the build-root-relative facts the engine needs (traps 1 and 2).
    The two are deliberately kept apart: for a root that is itself a package — the pinned
    Django benchmark analyzes `django/` — `relpath` is `db/models/query.py` while
    `module` is `django.db.models.query`.
    """

    path: Path
    relpath: str
    module: str
    source_root: Path
    content_hash: str


@dataclass(frozen=True, slots=True)
class BuildOutcome:
    """One `mypy.build.build()` call's result, in the vocabulary the adapter uses.

    * `sources` — the files actually handed to the engine, sorted by `relpath`.
    * `graph` / `types` — `BuildResult.graph` and `BuildResult.types`, untouched.
    * `rechecked_modules` — the build manager's rechecked-modules report **verbatim**
      (D6 rule 1). Never derive this from `State.tree` (trap 3).
    * `skipped` — per-file failures as `SkipRecord`s (FR-6): files that would not parse,
      would not decode, claimed a module name another file already had, or that the
      engine returned no usable state for.
    * `content_hashes` — sha256 of every file's bytes *as read*, a by-product of the
      pre-flight and the value `FileRecord.content_hash` carries (FR-24's hash gate).
    * `cache_fallback` — True when the AC-24.3 wipe-and-rebuild ran.
    """

    sources: tuple[EngineSource, ...]
    graph: Mapping[str, State]
    types: Mapping[object, object]
    rechecked_modules: frozenset[str]
    skipped: tuple[SkipRecord, ...]
    content_hashes: Mapping[str, str]
    engine_meta: Mapping[str, str]
    cache_fallback: bool = False

    def source_for(self, relpath: str) -> EngineSource | None:
        """The engine source for a root-relative path, or None when it was not analyzed."""
        for source in self.sources:
            if source.relpath == relpath:
                return source
        return None

    def state(self, relpath: str) -> State | None:
        """The engine's module state for `relpath`, or None when it has none."""
        source = self.source_for(relpath)
        return None if source is None else self.graph.get(source.module)

    def tree(self, relpath: str) -> MypyFile | None:
        """The typed AST for `relpath`.

        None means *no tree in this build* — which on a warm build is the ordinary case
        for a module that was not re-type-checked (trap 3), not a failure. Callers decide
        what to extract from `rechecked_modules`, never from this being non-None.
        """
        state = self.state(relpath)
        return None if state is None else state.tree

    def rechecked_sources(self) -> tuple[EngineSource, ...]:
        """This run's re-extraction set, as sources (D6 rule 1)."""
        return tuple(s for s in self.sources if s.module in self.rechecked_modules)


# ---------------------------------------------------------------------------
# Options (design.md §3.5, normative)
# ---------------------------------------------------------------------------


def build_options(cache_dir: Path, source_roots: Iterable[Path]) -> Options:
    """The normative §3.5 settings, plus the display suppression D9's stdout needs.

    `no_site_packages` together with the tool's own isolated environment is what makes
    AC-13.1 true — analysis does not depend on the target project's runtime environment —
    and what keeps results the same across machines. Missing imports are ignored for
    every module rather than reported: design.md §3.5 words this as a per-module setting
    for `*`, and a literal `"*"` key in `per_module_options` is inert in mypy 2.3.0
    (measured — it is treated as a concrete module name and so matches nothing), so the
    equivalent global option is what carries the design's intent here.

    Type errors in target code are expected and never stop a build; they are not read.
    """
    options = Options()
    options.incremental = True
    options.cache_dir = str(cache_dir)
    options.export_types = True
    options.preserve_asts = True
    options.check_untyped_defs = True
    options.no_site_packages = True
    options.ignore_missing_imports = True
    options.follow_imports = "normal"
    options.mypy_path = [str(root) for root in source_roots]
    # D13: no parallelism anywhere in the pipeline. The default is already 0; setting it
    # explicitly means a future default change cannot silently introduce workers.
    options.num_workers = 0
    # Error *display* off: the run's stdout belongs to the report renderings (D9), and
    # target-code type errors are not this tool's output.
    options.error_summary = False
    options.color_output = False
    options.pretty = False
    options.show_traceback = False
    return options


# ---------------------------------------------------------------------------
# The pre-flight: read, decode, parse, name (FR-6, EC-12, traps 1 and 2)
# ---------------------------------------------------------------------------


def _crawl_up(path: Path) -> tuple[str, Path]:
    """`(importable module name, the root it is importable from)` for one file.

    The rule is Python's own: walk up while the directory is a package, and the first
    non-package directory is the source root. This is what puts the *parent* of an
    analyzed package on `mypy_path` (trap 1) without the caller having to know whether
    the analysis root happens to be a package.
    """
    parts: list[str] = []
    stem = path.stem
    if stem != "__init__":
        parts.append(stem)
    directory = path.parent
    while directory != directory.parent and (directory / "__init__.py").is_file():
        parts.append(directory.name)
        directory = directory.parent
    return ".".join(reversed(parts)), directory


def _decode(data: bytes) -> str:
    """Decode source bytes the way the engine will: by the declared encoding.

    Raises `UnicodeDecodeError`, `LookupError` or `SyntaxError` — EC-12's cases, all of
    which become an `encoding_error` skip.
    """
    encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
    return data.decode(encoding)


def _reason(exc: BaseException) -> str:
    """A one-line human reason for a per-file failure (AC-7.2 renders this)."""
    if isinstance(exc, SyntaxError):
        where = f" at line {exc.lineno}" if exc.lineno else ""
        return f"{exc.msg}{where}"
    return str(exc) or type(exc).__name__


def _preflight(source: SourceFile) -> tuple[EngineSource | None, SkipRecord | None, str | None]:
    """Read, hash, decode, parse and name one candidate.

    Returns `(engine source, skip record, content hash)`: exactly one of the first two is
    ever set, and the hash is set whenever the bytes were read at all — a skipped file
    still needs its `files` row (FR-24's gate, FR-38's check).
    """
    relpath = source.relpath
    try:
        data = source.path.read_bytes()
    except OSError as exc:
        return (
            None,
            SkipRecord(
                path=relpath,
                reason="engine_error",
                detail=f"cannot be read ({exc.strerror or exc})",
            ),
            None,
        )

    digest = hashlib.sha256(data).hexdigest()
    try:
        text = _decode(data)
    except (UnicodeDecodeError, LookupError, SyntaxError) as exc:
        # EC-12: a non-UTF-8 file with no declaration, or one declaring an encoding it
        # is not actually written in.
        return (
            None,
            SkipRecord(
                path=relpath,
                reason="encoding_error",
                detail=f"cannot be decoded as Python source ({_reason(exc)})",
            ),
            digest,
        )

    try:
        # The tree is discarded: this parse exists to answer "would the engine choke on
        # this file", which is a yes/no question (AC-6.1, EC-1, EC-2).
        ast.parse(text, filename=relpath)
    except (SyntaxError, ValueError) as exc:
        return (
            None,
            SkipRecord(
                path=relpath,
                reason="parse_error",
                detail=f"cannot be parsed as Python source ({_reason(exc)})",
            ),
            digest,
        )

    # A module name is not required to be a legal identifier: mypy 2.3.0 accepts any
    # name string, and requiring identifiers would drop every Django migration
    # (`0001_initial.py`) and packages like `django/conf/locale/is/` — 25 real files of
    # the pinned benchmark, measured. Two files claiming one name is the only naming
    # hazard, and `prepare_sources()` handles that one.
    module, source_root = _crawl_up(source.path)
    return (
        EngineSource(
            path=source.path,
            relpath=relpath,
            module=module,
            source_root=source_root,
            content_hash=digest,
        ),
        None,
        digest,
    )


def prepare_sources(
    files: Sequence[SourceFile], progress: ProgressSink | None = None
) -> tuple[list[EngineSource], list[SkipRecord], dict[str, str]]:
    """Pre-flight every candidate, splitting them into engine inputs and skips.

    Two files claiming one module name are a per-file failure for the loser, not a run
    failure: mypy rejects a duplicate module name as a build blocker, which would cost
    the whole run. Files are processed in `relpath` order, so which file wins is stable
    across runs (FR-44).
    """
    ordered = sorted(files, key=lambda source: source.relpath)
    sources: list[EngineSource] = []
    skipped: list[SkipRecord] = []
    hashes: dict[str, str] = {}
    claimed: dict[str, EngineSource] = {}

    if progress is not None:
        progress.start_phase(PHASE_READ, total=len(ordered))
    try:
        for candidate in ordered:
            prepared, skip, digest = _preflight(candidate)
            if digest is not None:
                hashes[candidate.relpath] = digest
            if skip is not None:
                skipped.append(skip)
            elif prepared is not None:
                owner = claimed.get(prepared.module)
                if owner is not None:
                    skipped.append(
                        SkipRecord(
                            path=prepared.relpath,
                            reason="engine_error",
                            detail=(
                                f"resolves to the module name {prepared.module!r}, already "
                                f"claimed by {owner.relpath}; only one file per module name "
                                f"can be analyzed"
                            ),
                        )
                    )
                else:
                    claimed[prepared.module] = prepared
                    sources.append(prepared)
            if progress is not None:
                progress.advance()
    finally:
        if progress is not None:
            progress.end_phase()

    return sources, skipped, hashes


# ---------------------------------------------------------------------------
# The build (design.md §3.5; AC-24.3, AC-41.2)
# ---------------------------------------------------------------------------


def _invoke_build(sources: Sequence[BuildSource], options: Options) -> BuildResult:
    """The single call site of `mypy.build.build()` (design.md §3.5's enumerated API).

    Both engine streams are replaced with throwaway buffers so nothing the engine has to
    say can reach the run's stdout (D9) or its progress channel (FR-41). Errors are still
    collected on `BuildResult.errors`; nothing reads them, because target-code type
    errors are an expected product of analyzing real code, not this tool's findings.
    """
    return _build(list(sources), options, stdout=io.StringIO(), stderr=io.StringIO())


def _wipe_cache(cache_dir: Path) -> None:
    """Discard the engine's incremental cache (AC-24.3's fallback precondition)."""
    shutil.rmtree(cache_dir, ignore_errors=True)


def _engine_message(exc: BaseException) -> str:
    """The engine's own words for a whole-build failure."""
    messages = getattr(exc, "messages", None)
    if messages:
        return "; ".join(str(message) for message in messages)
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


def _run_engine(
    sources: Sequence[EngineSource], cache_dir: Path, progress: ProgressSink
) -> tuple[BuildResult, bool]:
    """Build once; on a whole-build failure, wipe the cache and build once more.

    design.md §3.5 is normative on the shape: a *whole-build* crash (as opposed to a
    per-file failure, which the pre-flight already turned into a skip) gets exactly one
    full-rebuild fallback with the cache dir wiped, and if that also fails the run fails
    with the engine error surfaced. One retry, never a loop: a corrupt cache is
    recoverable, a broken engine is not, and the difference between them is precisely
    whether a clean cache helps.

    A fresh `Options` is built for the retry — mypy documents an `Options` object as
    read-only once per-module cloning has touched it.
    """
    roots = sorted({source.source_root for source in sources})
    build_sources = [BuildSource(str(source.path), source.module, None) for source in sources]

    try:
        with progress.heartbeat(PHASE_BUILD):
            return _invoke_build(build_sources, build_options(cache_dir, roots)), False
    except Exception as first:
        first_message = _engine_message(first)

    # AC-24.3 / AC-30.2: never silent. The user is told a longer run is under way while
    # it is under way, and `cache_fallback` carries the fact into the re-analysis report.
    progress.note(FALLBACK_NOTICE)
    _wipe_cache(cache_dir)
    try:
        with progress.heartbeat(PHASE_BUILD):
            return _invoke_build(build_sources, build_options(cache_dir, roots)), True
    except Exception as second:
        raise EngineError(
            f"the analysis engine failed to build this codebase: {_engine_message(second)}"
            f" (a full rebuild with a discarded cache was already attempted after: "
            f"{first_message})"
        ) from second


def _sorted_skips(skipped: Iterable[SkipRecord]) -> tuple[SkipRecord, ...]:
    """Skips in path order, so two runs over one tree report them identically (FR-44)."""
    return tuple(sorted(skipped, key=lambda record: record.path))


def run_build(files: Sequence[SourceFile], cache_dir: Path, progress: ProgressSink) -> BuildOutcome:
    """Drive one engine build over `files`, returning the §3.5 outcome.

    Raises `EngineError` only when the engine failed the whole build twice (see
    `_run_engine`). Every other failure is per-file and arrives as a `SkipRecord`, so a
    codebase with one unparseable file is analyzed anyway (AC-6.1) and a codebase where
    *every* file fails still returns — with no engine call at all, since there would be
    nothing to build — so the run completes and says so (AC-6.2).
    """
    sources, skipped, hashes = prepare_sources(files, progress)
    engine_meta = {"engine": ENGINE_NAME, "engine_version": ENGINE_VERSION}

    if not sources:
        return BuildOutcome(
            sources=(),
            graph={},
            types={},
            rechecked_modules=frozenset(),
            skipped=_sorted_skips(skipped),
            content_hashes=hashes,
            engine_meta=engine_meta,
        )

    result, cache_fallback = _run_engine(sources, cache_dir, progress)

    kept: list[EngineSource] = []
    for source in sources:
        state = result.graph.get(source.module)
        if state is None:
            # The engine returned no state for a file it was handed. This is not the
            # warm-cache case (that is a state with no tree, trap 3) — it is the engine
            # declining the file, which is a per-file failure under FR-6.
            skipped.append(
                SkipRecord(
                    path=source.relpath,
                    reason="engine_error",
                    detail="the analysis engine produced no module state for this file",
                )
            )
            continue
        resolved = state.abspath or state.path
        if resolved is not None and Path(resolved) != source.path:
            # The module name resolved to a different file (a shadowed name). Extracting
            # it would attribute another file's code to this one.
            skipped.append(
                SkipRecord(
                    path=source.relpath,
                    reason="engine_error",
                    detail=(
                        f"the module name {source.module!r} resolved to {resolved} rather "
                        f"than this file; it is shadowed by another module"
                    ),
                )
            )
            continue
        kept.append(source)

    return BuildOutcome(
        sources=tuple(kept),
        graph=result.graph,
        types=result.types,
        # D6 rule 1: the build manager's own report, verbatim. Never tree presence.
        rechecked_modules=frozenset(result.manager.rechecked_modules),
        skipped=_sorted_skips(skipped),
        content_hashes=hashes,
        engine_meta=engine_meta,
        cache_fallback=cache_fallback,
    )
