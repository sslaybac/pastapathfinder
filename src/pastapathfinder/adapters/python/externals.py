"""External leaf nodes from D15's two sources (design.md §3.5, D15; FR-36, FR-14).

design.md §3.5 (`externals`), §4.1 (node-ID grammar), §4.2 (DDL, AC-36.5's dedup key),
§4.3 (`Diag`), D15, D23; requirements FR-36 (AC-36.1, AC-36.2, AC-36.4, AC-36.5),
FR-14 (AC-14.2's fallthrough), FR-37 (AC-37.2), §6 item 10 as amended 2026-07-18.

A call whose target is not itself analyzed still has to appear in the graph, or a trace
would end without saying why (FR-36, EC-3). This module mints the leaf node it ends at.
D15 gives it two sources, and the split matters because they fail differently:

**(a) The engine named the target, and the name left the analyzed set.** A typeshed
resolution (`os.getcwd`), a class the ladder stopped at because it lives outside the set
(D23(b)), or a function in a file this run excluded or skipped (AC-36.2, and note that
mypy still reads such a file off disk, so it is usually *named* rather than unresolved).
`extract.py` hands these over as `ExternalCall` records — already collapsed per
`(src, qualified name)` and already carrying their FR-40 ambiguity flag — because the edge
could not be written there: its `dst` node does not exist until this module mints it.

**(b) The engine resolved nothing, but the callee is a name imported from an unanalyzed
module.** `from ext import thing` followed by `thing()` is invisible to a soundness
checker when `ext` is not installed — mypy assigns `Any` and offers no target — while the
import statement names the target exactly. The per-module import table below reads that
statement and supplies `ext.thing`.

**Why (b) is in scope at all, and how far it goes.** Requirements §6 item 10 excludes
writing novel static analysis; its 2026-07-18 amendment exempts "bounded syntactic
mechanisms built on the standard library's parser — specifically, import/symbol-table
resolution used to attribute calls to external or unanalyzed targets (FR-36)". This module
is that mechanism and nothing more: the table is built with stdlib `ast` (design.md §3.5,
amended 2026-07-22 — never from the engine's trees, which do not exist for a file the
engine did not recheck), and a name that no import statement binds yields **no node**. The
evidence for the exemption is `FINDINGS-namematch.md` §2, whose `external` row measured
100 % precision (6 TP / 0 FP) for exactly this lookup — and the *reason* the exemption
stops here: the same probe's method-name matching fanned one `super().__init__()` out to
760 classes and Django's graph to 23× its size. Narrowed method dispatch is backlog B-22,
out of v1 scope; a callee this module cannot name stays an `unresolved_call` diagnostic
(AC-36.4) rather than becoming a guess.

**Nothing is fabricated, and nothing is silently dropped.** Every call site arriving here
leaves as one of three things: an edge to a minted node, a retained `unresolved_call`
diagnostic, or — where a qualified name turns out not to be expressible as a §4.1 ID — a
freshly written `unresolved_call` diagnostic naming it. External nodes carry
`is_external=1`, no file path, no span (AC-37.2), and never a single outgoing edge; the
last is enforced here rather than merely intended, because a leaf that acquired an edge
would let a slice walk into code FR-36 says was never analyzed.
"""

from __future__ import annotations

import ast
from collections.abc import Container, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pastapathfinder.adapters.python.extract import CallResolution, ExternalCall
from pastapathfinder.adapters.python.mypy_driver import EngineSource
from pastapathfinder.schema import (
    LANGUAGE_PYTHON,
    Diag,
    EdgeRow,
    NodeRow,
    is_valid_node_id,
)

#: The `nodes.kind` an external leaf carries. Every external node exists because something
#: *called* it, so `function` is the accurate member of §4.2's five — `file` and `module`
#: name compilation units this tool never opened, and `entry_point` belongs to the
#: detectors. D23(b) makes a minority of these names classes (`builtins.ValueError`), and
#: the qualified name is all this module is given, so they are labelled `function` too:
#: `is_external=1` is the field that carries what is actually known about them, and FR-36
#: asks for a leaf marked external carrying the best resolvable qualified name, not for a
#: classification of code that was deliberately not analyzed.
EXTERNAL_KIND = "function"


# ---------------------------------------------------------------------------
# The node (FR-36, AC-36.5, AC-37.2)
# ---------------------------------------------------------------------------


def external_node_id(qualified_name: str) -> str:
    """§4.1's external form: `python:<qualified name>`, the AC-36.5 dedup key."""
    return f"{LANGUAGE_PYTHON}:{qualified_name}"


def external_node(qualified_name: str) -> NodeRow:
    """The leaf node for one external symbol.

    One row per qualified name, by construction: the ID *is* the name, so two callers of
    `os.getcwd` mint identical rows and the store's canonical-sort layer collapses them
    (AC-36.5). `name` is the last segment — the way the symbol is written at the call site
    — while the ID keeps the qualification that makes it unique.

    No `file_path`, no span: FR-36 forbids analyzing an external target's internals, so
    there is no source location to point at, and AC-37.2 requires the absence rather than
    a placeholder.
    """
    return NodeRow(
        id=external_node_id(qualified_name),
        kind=EXTERNAL_KIND,
        name=qualified_name.rpartition(".")[2] or qualified_name,
        language=LANGUAGE_PYTHON,
        file_path=None,
        start_line=None,
        end_line=None,
        is_external=1,
    )


# ---------------------------------------------------------------------------
# The import table (D15 source (b); stdlib `ast` only)
# ---------------------------------------------------------------------------


def absolute_module(importer: str, is_package: bool, level: int, target: str | None) -> str | None:
    """The absolute module an import statement names, resolving `from . import x` forms.

    `importer` is the module name the *engine* imports the file as, because that is the
    namespace the file's own import statements are written against; `level` and `target`
    are `ast.ImportFrom`'s. Returns None when the statement climbs above the root, which
    is a broken import rather than something to name.
    """
    if not level:
        return target or None
    parts = importer.split(".")
    if not is_package:
        parts = parts[:-1]
    ascend = level - 1
    if ascend:
        if ascend > len(parts):
            return None
        parts = parts[: len(parts) - ascend]
    if target:
        parts.append(target)
    return ".".join(part for part in parts if part) or None


@dataclass(frozen=True, slots=True)
class ImportBinding:
    """A name an import statement binds, and what the statement proves about it.

    `qualified` is the name the binding refers to. `module_depth` is how many of its
    leading dot-separated segments the statement itself proved to be a *module* path —
    `from a.b import c` proves `a.b`, so 2; `import a.b.c` binds `a` and proves only that,
    so 1. That number is what keeps the analyzed-set test honest in both directions
    (`inside_analyzed_set`), and it exists because Python's syntax gives no other way to
    tell where a module path stops and an attribute path starts.
    """

    qualified: str
    module_depth: int


def import_table(tree: ast.AST, module: str, is_package: bool) -> dict[str, ImportBinding]:
    """`{bound name: ImportBinding}` for every import statement in one module.

    Built from a stdlib `ast` parse and nothing else (design.md §3.5, amended 2026-07-22),
    so it is available for any file that parses — including one the engine produced no
    tree for, which is the ordinary case on a warm build.

    Three decisions, each visible in the output:

    * **Every scope, not only the module body.** Deferred imports inside functions are how
      real code breaks cycles (Django is full of them), and a call to such a name is
      exactly the shape mypy tends to leave unresolved. A module-body-only table would
      miss precisely the cases this mechanism exists for.
    * **`import a.b.c` binds `a`.** That is Python's own rule; a callee written
      `a.b.c.thing` is rebuilt from the binding plus the attribute path it was written
      with, so the qualified name comes out the same either way.
    * **A name bound twice to different things is dropped.** Two statements disagreeing
      about what a name means make the lookup a guess, and AC-36.4 forbids guessing.
    """
    bound: dict[str, ImportBinding | None] = {}

    def bind(name: str, qualified: str, module_depth: int) -> None:
        binding = ImportBinding(qualified, module_depth)
        if name in bound and bound[name] != binding:
            bound[name] = None
        else:
            bound[name] = binding

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    bind(alias.asname, alias.name, alias.name.count(".") + 1)
                else:
                    head = alias.name.partition(".")[0]
                    bind(head, head, 1)
        elif isinstance(node, ast.ImportFrom):
            base = absolute_module(module, is_package, node.level, node.module)
            if base is None:
                continue
            depth = base.count(".") + 1
            for alias in node.names:
                if alias.name == "*":
                    # A star import binds names this table cannot enumerate without
                    # reading the other module. Nothing is bound, so nothing is claimed.
                    continue
                bind(alias.asname or alias.name, f"{base}.{alias.name}", depth)

    return {name: binding for name, binding in bound.items() if binding is not None}


def read_import_table(path: Path, module: str, is_package: bool) -> dict[str, ImportBinding] | None:
    """`import_table()` for a file on disk, or None when it cannot be read or parsed.

    `ast.parse` is handed the raw bytes so the file's own encoding declaration governs,
    the same way the engine's pre-flight read it.

    None is not an error: the file parsed once already (nothing reaches this module
    otherwise), so a failure here means it changed or vanished under the run — EC-14's
    window, which FR-38's post-run check is what reports. The caller keeps its unresolved
    diagnostics, which is the honest outcome: the sites stay unattributed rather than
    being attributed from a file whose contents no longer match the analysis.
    """
    try:
        return import_table(ast.parse(path.read_bytes()), module, is_package)
    except (OSError, SyntaxError, ValueError):
        return None


def qualify(callee: str, table: Mapping[str, ImportBinding]) -> ImportBinding | None:
    """What an imported callee names, or None when no import statement binds it.

    `callee` is the reconstructed call-site text `extract.py` records on every
    `unresolved_call` diagnostic (design.md §4.3). Only a plain dotted name can be looked
    up: anything the reconstruction wrote with parentheses or brackets (`factory(...).run`,
    `handlers[...]`) is a value, not a name, and has no entry to find. The attribute path
    written at the call site is appended to the binding, so `import acme.legacy as legacy`
    and `import acme.legacy` name the same symbol from `legacy.run()` and
    `acme.legacy.run()`.
    """
    parts = callee.split(".")
    if not all(part.isidentifier() for part in parts):
        return None
    binding = table.get(parts[0])
    if binding is None:
        return None
    return ImportBinding(".".join([binding.qualified, *parts[1:]]), binding.module_depth)


def inside_analyzed_set(binding: ImportBinding, analyzed_modules: Container[str]) -> bool:
    """True when an imported name lies inside a module this run analyzed.

    `analyzed_modules` holds the *engine's* module names for the analyzed set, which is
    the namespace import statements are written in. A name under one of them belongs to
    code this run did analyze, so naming it externally would be a fabrication of the kind
    AC-36.4 forbids — the site stays unresolved instead, and the loss stays visible.

    Only prefixes at least as long as `module_depth` are tested, and that is the whole
    subtlety. `from pkg.hidden import work`, in a run where `pkg/__init__.py` is analyzed
    and `pkg/hidden.py` is excluded, proves `pkg.hidden` is a module — so the analyzed
    `pkg` above it must not suppress the external node AC-36.2 asks for. Testing from
    segment one instead would silently swallow every call into an excluded or skipped file
    that happens to sit inside an analyzed package, which in a real tree is most of them.
    """
    parts = binding.qualified.split(".")
    return any(
        ".".join(parts[:cut]) in analyzed_modules
        for cut in range(binding.module_depth, len(parts) + 1)
    )


# ---------------------------------------------------------------------------
# What this module produces
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FileExternals:
    """One caller file's external leaves, its edges to them, and what stayed unresolved.

    Attributed per caller file because that is how the graph is written and evicted: a
    fragment carries its file's nodes and its outgoing edges (design.md §4.3), and D6
    rule 3 keys eviction on the caller file. Two files calling one external symbol
    therefore both carry the node; the rows are identical, so the store keeps one
    (AC-36.5).

    `diagnostics` **replaces** the resolution's `unresolved_call` list rather than adding
    to it: a site source (b) named is no longer unresolved, and one whose name could not
    be expressed as a §4.1 ID becomes a new diagnostic here.
    """

    nodes: tuple[NodeRow, ...] = ()
    edges: tuple[EdgeRow, ...] = ()
    diagnostics: tuple[Diag, ...] = ()


@dataclass(slots=True)
class _Aggregate:
    """The call sites collapsing onto one `(caller scope, external name)` edge (§4.2)."""

    sites: set[tuple[int, int]] = field(default_factory=set)
    is_ambiguous: bool = False
    src_file: str | None = None

    def add(self, sites: Iterable[tuple[int, int]], ambiguous: bool, src_file: str | None) -> None:
        self.sites.update(sites)
        # As in `extract.py`: an edge aggregating several sites is ambiguous if any of
        # them was. Over-claiming ambiguity is the direction FR-14/FR-40 tolerate.
        self.is_ambiguous = self.is_ambiguous or ambiguous
        self.src_file = self.src_file or src_file

    def attrs(self) -> dict[str, Any]:
        return {"call_sites": [[line, col] for line, col in sorted(self.sites)]}


def _unnameable(src: str, qualified_name: str, aggregate: _Aggregate) -> list[Diag]:
    """AC-14.2 diagnostics for a name that cannot be written as a §4.1 node ID.

    §4.1 says an external ID collides with no analyzed node "by definition", and D22's
    module-segment production accepts almost anything a real qualified name contains — so
    this is a corner, not a routine path. It is still handled rather than assumed away:
    dropping the site would be the one thing FR-14 rules out, so the site reappears as an
    unresolved diagnostic carrying the name that could not be used.
    """
    message = (
        f"call target {qualified_name!r} lies outside the analyzed set but cannot be "
        f"written as a node identifier; no external node was created for it"
    )
    positions: Sequence[tuple[int, int] | tuple[None, None]] = sorted(aggregate.sites) or [
        (None, None)
    ]
    return [
        Diag(
            kind="unresolved_call",
            path=aggregate.src_file,
            line=line,
            col=col,
            message=message,
            extra={"callee": qualified_name, "scope": src},
        )
        for line, col in positions
    ]


def _recovered(diag: Diag, qualified_name: str) -> ExternalCall | None:
    """Turn a source-(b) hit into the same record shape source (a) arrives in."""
    scope = diag.extra.get("scope")
    if not isinstance(scope, str):  # pragma: no cover - extract.py always records it
        return None
    sites = () if diag.line is None or diag.col is None else ((diag.line, diag.col),)
    return ExternalCall(
        src=scope,
        qualified_name=qualified_name,
        src_file=diag.path or "",
        call_sites=sites,
        # One import statement names one target, so a recovered site is unambiguous
        # (AC-40.2). Fanning a name out to every same-named definition is the B-22
        # mechanism this one deliberately is not.
        is_ambiguous=0,
    )


def resolve_file(
    source: EngineSource,
    resolution: CallResolution,
    analyzed_modules: Container[str],
) -> FileExternals:
    """Mint one file's external leaf nodes and the `calls` edges reaching them.

    Source (a) arrives in `resolution.external_calls`; source (b) is recovered from the
    `unresolved_call` diagnostics whose callee an import statement binds. The file is
    parsed for its import table only if it has a diagnostic worth looking up — design.md
    §3.5's costing ("only files carrying unresolved sites need one") — so a fully resolved
    file costs nothing.

    A *partially* resolved site (one that produced an edge and still lost a candidate,
    marked by `extra.unmapped`) is left alone: D15 source (b) is for sites the engine left
    unresolved, and the loss there is a definition the index lacks, not an import.
    """
    aggregates: dict[tuple[str, str], _Aggregate] = {}

    def collect(call: ExternalCall) -> None:
        aggregates.setdefault((call.src, call.qualified_name), _Aggregate()).add(
            call.call_sites, bool(call.is_ambiguous), call.src_file or source.relpath
        )

    for call in resolution.external_calls:
        collect(call)

    retained: list[Diag] = []
    table: dict[str, ImportBinding] | None = None
    consulted = False
    for diag in resolution.diagnostics:
        callee = diag.extra.get("callee")
        engine_left_it = diag.kind == "unresolved_call" and "unmapped" not in diag.extra
        if not engine_left_it or not isinstance(callee, str):
            retained.append(diag)
            continue
        if not consulted:
            consulted = True
            table = read_import_table(
                source.path, source.module, source.relpath.endswith("__init__.py")
            )
        imported = None if table is None else qualify(callee, table)
        if imported is None or inside_analyzed_set(imported, analyzed_modules):
            # AC-36.4: no import binds the callee (or it binds one of our own modules, in
            # which case the target is analyzed and an external node would be a lie).
            retained.append(diag)
            continue
        recovered = _recovered(diag, imported.qualified)
        if recovered is None:  # pragma: no cover - defensive
            retained.append(diag)
        else:
            collect(recovered)

    edges: list[EdgeRow] = []
    minted: set[str] = set()
    for (src, qualified_name), aggregate in sorted(aggregates.items()):
        node_id = external_node_id(qualified_name)
        if not is_valid_node_id(node_id):
            retained.extend(_unnameable(src, qualified_name, aggregate))
            continue
        minted.add(qualified_name)
        edges.append(
            EdgeRow(
                src=src,
                dst=node_id,
                kind="calls",
                src_file=aggregate.src_file or source.relpath,
                is_ambiguous=int(aggregate.is_ambiguous),
                attrs=aggregate.attrs(),
            )
        )

    # FR-36's leaf guarantee, enforced rather than intended: nothing this module emits may
    # leave an external node. The edges are built from caller scopes, so a violation is a
    # pipeline bug (a call site whose enclosing scope *is* an external name), and it fails
    # loudly here rather than becoming a slice that walks into unanalyzed code.
    escaping = sorted({edge.src for edge in edges} & {external_node_id(n) for n in minted})
    if escaping:
        raise ValueError(
            f"external leaf nodes must have no outgoing edges (FR-36); {source.relpath}: {escaping}"
        )

    # D12's canonical order, applied where the rows are made: nodes by id, edges by the
    # `(src, dst)` the aggregate keys already sorted on.
    nodes = tuple(external_node(qualified_name) for qualified_name in sorted(minted))
    return FileExternals(nodes=nodes, edges=tuple(edges), diagnostics=tuple(retained))


def resolve(
    entries: Sequence[tuple[EngineSource, CallResolution]],
    analyzed_modules: Iterable[str],
) -> dict[str, FileExternals]:
    """`resolve_file()` over a whole run's re-extracted files, keyed by root-relative path.

    `analyzed_modules` is the engine's module name for **every** analyzed file, not only
    the re-extracted ones: whether a name leaves the analyzed set is a question about the
    run's inputs, and an incremental run that only rechecked one file must not start
    calling the rest of the codebase external (D6).
    """
    analyzed = set(analyzed_modules)
    return {
        source.relpath: resolve_file(source, resolution, analyzed) for source, resolution in entries
    }


__all__ = [
    "EXTERNAL_KIND",
    "FileExternals",
    "absolute_module",
    "external_node",
    "external_node_id",
    "import_table",
    "inside_analyzed_set",
    "qualify",
    "read_import_table",
    "resolve",
    "resolve_file",
]
