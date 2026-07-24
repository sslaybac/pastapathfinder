"""Slices, reachability, and dead code — the flagship queries (design.md §3.9).

design.md §3.9 (`queries`, normative), §4.2 (the tables read), §5.2 (the wire shapes both
surfaces emit), §5.3 (`deadcode.json`'s shape), D5 (recursive CTEs), D12 (determinism),
D16 (the sliceable kinds and the `function`-only dead-code report), D19 (derived
reachability), D20 (dead code recomputed from the index, never read from a report file),
§8-O2 (the provisional slice bound); requirements FR-15 (AC-15.1/15.2), FR-16
(AC-16.1/16.2), FR-17 (AC-17.1/17.2), FR-18 (AC-18.1/18.2), FR-19 (AC-19.1-19.3), FR-20
(AC-20.1/20.2), FR-28 (AC-28.2), FR-36 (AC-36.3), EC-6, EC-9.

Everything here answers from the index alone (AC-20.1): no engine, no source tree, no
report file. One code path serves both surfaces — `query` on the CLI (task 3.5) and the
viewer's HTTP API (task 5.1) — which is what keeps the two from drifting.

**The serialization lives here for the same reason the queries do.** design.md §5.2 fixes
one set of JSON shapes and §5.1 has `--json` emit "the same structured shapes as the HTTP
API"; two renderings of one shape would drift the first time a field moved. So the
`*_json()` functions below are the single producer, and both surfaces are thin: the CLI
prints what they return, the server (task 5.1) serves it. They live in this module rather
than in `cli` because the viewer may import neither `adapters` nor anything that reaches
the engine (AC-25.1), and `cli` reaches `runner` and through it the adapter.

**Reachability is one edge kind.** The BFS follows `calls` and nothing else: `imports`
edges say a file was imported, not that anything in it ran, and `contains` edges are
structure. That is why D19 needs a second pass at all, and why what it writes is a
*derivation* rather than a graph result — the docstrings say so at each site, because that
column is the one a future consumer is most likely to misread.

**Determinism.** Two calls with the same arguments against the same index return the same
objects in the same order. SQLite's own row order is never load-bearing here: slice nodes
come back in `(depth, id)` order — BFS order, made total by the id tiebreak — and edges in
the store's canonical `(src, dst, kind)` order, both imposed in this module rather than
inherited from a query plan.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pastapathfinder.index import Index, IndexIncompatibleError, IndexStoreError
from pastapathfinder.reports import FORMAT_VERSION
from pastapathfinder.schema import DEADCODE_CAVEAT, EdgeRow, NodeRow

# ---------------------------------------------------------------------------
# Vocabulary (design.md §3.9)
# ---------------------------------------------------------------------------

#: The two slice directions of §5.1/§5.2. Forward follows `src→dst` (callees), backward
#: `dst→src` (callers) — one traversal, two column orders (D5).
FORWARD = "forward"
BACKWARD = "backward"
DIRECTIONS: tuple[str, ...] = (FORWARD, BACKWARD)

#: The node kinds a slice may start from (D16). `file` is deliberately absent: a file node
#: carries no `calls` edges, so slicing from one is undefined rather than empty (AC-17.2).
SLICEABLE_KINDS: tuple[str, ...] = ("entry_point", "function", "class", "module")

#: The kinds `reachable` is written on (design.md §4.2's column comment): BFS-computed on
#: `function`, derived on `class` and `module` (D19). Every other kind stays NULL.
REACHABLE_KINDS: tuple[str, ...] = ("function", "class", "module")

#: The slice's node budget. **Provisional** (design.md §8-O2): it exists to satisfy
#: AC-28.2's bound-it-visibly mandate and is tuned when OQ-4 resolves against real graphs
#: at the viewer milestone. This is its single definition site — the CLI's `--max-nodes`
#: default and the API's `max_nodes` default both resolve here, so re-tuning it is one
#: edit, not a search.
SLICE_MAX_NODES = 200

#: Columns of `nodes`, in the order `_node_row()` unpacks them.
_NODE_COLUMNS = (
    "id, kind, name, language, file_path, start_line, end_line, is_external, reachable, attrs"
)

#: Columns of `edges`, likewise.
_EDGE_COLUMNS = "src, dst, kind, src_file, is_ambiguous, attrs"

#: §4.2's reserved `attrs` key naming the detector that emitted an entry node. Spelled
#: here rather than imported from `detectors.base`, which reaches `adapters.python` for the
#: ID grammar and would therefore pull the engine into the import graph of every consumer
#: of this module — including the viewer, which may reach neither (AC-25.1).
ATTR_DETECTOR = "detector"

#: SQLite's parameter ceiling is generous but finite, and `max_nodes` is caller-supplied;
#: id lists are therefore bound in chunks rather than in one statement.
_CHUNK = 500


# ---------------------------------------------------------------------------
# Errors (AC-16.2, AC-17.2)
# ---------------------------------------------------------------------------


class QueryError(Exception):
    """Base class for a query that cannot be answered. `cli.main()` maps these to 2."""


class UnknownNodeError(QueryError):
    """No node with that identifier is in the index (AC-16.2, EC-15).

    Names the identifier: a slice is requested with an ID the user — or a viewer view left
    over from before a re-analysis — is holding, and "not found" without saying what was
    not found is unactionable.
    """

    def __init__(self, node_id: str) -> None:
        self.node_id = node_id
        super().__init__(
            f"unknown node {node_id!r}; it is not in this index — re-run "
            f"`pastapathfinder analyze` if the codebase has changed since it was written"
        )


class NotSliceableError(QueryError):
    """The node exists but its kind has no slice defined (AC-17.2).

    Names the kind, because the answer is not "nothing found" — it is "this kind of node
    is not a slice origin", and the two must not look alike.
    """

    def __init__(self, node_id: str, kind: str) -> None:
        self.node_id = node_id
        self.kind = kind
        super().__init__(
            f"node {node_id!r} is a {kind!r} node, which is not sliceable; "
            f"slice from one of {list(SLICEABLE_KINDS)}"
        )


#: design.md §5.2's error vocabulary, exhaustive. Both surfaces classify a failed query
#: with `error_code()` below, so a consumer can branch on the code rather than on prose.
ERROR_UNKNOWN_NODE = "unknown_node"
ERROR_NOT_SLICEABLE = "not_sliceable"
ERROR_INDEX_MISSING = "index_missing"
ERROR_INDEX_INCOMPATIBLE = "index_incompatible"
ERROR_CODES: tuple[str, ...] = (
    ERROR_UNKNOWN_NODE,
    ERROR_NOT_SLICEABLE,
    ERROR_INDEX_MISSING,
    ERROR_INDEX_INCOMPATIBLE,
)


def error_code(exc: Exception) -> str:
    """The §5.2 code for a failed query.

    The four codes are the whole vocabulary §5.2 defines, so the mapping is total by
    construction: `IndexIncompatibleError` is the versioned refusal (AC-39.2, EC-13) and
    every other store failure — absent, or present but not openable — is `index_missing`,
    which is what it means to a consumer: there is no index here to answer from. The
    distinction survives in the message, which always names the path and the reason.
    """
    if isinstance(exc, NotSliceableError):
        return ERROR_NOT_SLICEABLE
    if isinstance(exc, UnknownNodeError):
        return ERROR_UNKNOWN_NODE
    if isinstance(exc, IndexIncompatibleError):
        return ERROR_INDEX_INCOMPATIBLE
    if isinstance(exc, IndexStoreError):
        return ERROR_INDEX_MISSING
    raise ValueError(f"{type(exc).__name__} is not a §5.2 query failure: {exc}")


# ---------------------------------------------------------------------------
# Result shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SliceResult:
    """One slice (design.md §3.9): the subgraph, plus how it was bounded.

    `nodes` is in BFS order from the origin — `(depth, id)`, so it is total and stable —
    and includes the origin itself. `edges` are the `calls` edges with both endpoints in
    `nodes`, in the store's canonical order. `truncated` and `frontier` are AC-28.2's
    visible bound: `frontier` names the nodes the traversal reached but the budget
    excluded, so a caller expands deliberately instead of wondering what it lost.
    """

    nodes: list[NodeRow] = field(default_factory=list)
    edges: list[EdgeRow] = field(default_factory=list)
    truncated: bool = False
    frontier: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class EntryPoint:
    """One detected entry point, in the fields design.md §5.2 publishes for it.

    `target_id` is the node the entry drives, read from its single outgoing `calls` edge
    (§3.7) rather than from `attrs`: the edge is what reachability traverses, so listing
    the edge's target is listing what the entry actually reaches. It is `None` only for an
    entry whose edge is missing — which fragment validation rejects at the write boundary
    (AC-23.2), so it stands for a corrupt index rather than a supported state.
    """

    id: str
    name: str
    detector: str
    target_id: str | None = None
    file_path: str | None = None
    start_line: int | None = None


@dataclass(frozen=True, slots=True)
class ReachabilityResult:
    """What one `reachability()` pass wrote, and EC-9's warning flag.

    `no_entry_points` is AC-18.2's: reachability is still computed with zero entry points
    — the run must not fall silent — but every result it produced is uninformative, and
    the run output has to say so rather than present a codebase-wide dead-code list.
    """

    entry_points: int = 0
    reachable_functions: int = 0

    @property
    def no_entry_points(self) -> bool:
        return self.entry_points == 0


@dataclass(frozen=True, slots=True)
class DeadCodeFunction:
    """One unreachable function in the dead-code report (§5.3)."""

    id: str
    name: str
    start_line: int | None = None

    def as_json(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "start_line": self.start_line}


@dataclass(frozen=True, slots=True)
class DeadCodeGroup:
    """The unreachable functions of one file — FR-19's grouping (AC-19.1)."""

    file: str
    functions: list[DeadCodeFunction] = field(default_factory=list)

    def as_json(self) -> dict[str, Any]:
        return {"file": self.file, "functions": [function.as_json() for function in self.functions]}


@dataclass(frozen=True, slots=True)
class DeadCodeResult:
    """`dead_code()`'s answer: the grouped findings, inseparable from their caveat.

    `caveat` rides on the result rather than being left to each renderer to remember:
    AC-19.2 requires it in *every* presentation, and the only way to guarantee that is for
    the thing being presented to contain it.
    """

    unreachable: list[DeadCodeGroup] = field(default_factory=list)
    no_entry_points_warning: bool = False
    caveat: str = DEADCODE_CAVEAT


# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------


def _attrs(value: object) -> dict[str, Any]:
    """Parse an `attrs` column back into a mapping (D4's JSON column)."""
    if not value:
        return {}
    parsed = json.loads(str(value))
    return parsed if isinstance(parsed, dict) else {}


def _node_row(row: Sequence[Any]) -> NodeRow:
    return NodeRow(
        id=str(row[0]),
        kind=str(row[1]),
        name=str(row[2]),
        language=str(row[3]),
        file_path=None if row[4] is None else str(row[4]),
        start_line=None if row[5] is None else int(row[5]),
        end_line=None if row[6] is None else int(row[6]),
        is_external=int(row[7]),
        reachable=None if row[8] is None else int(row[8]),
        attrs=_attrs(row[9]),
    )


def _edge_row(row: Sequence[Any]) -> EdgeRow:
    return EdgeRow(
        src=str(row[0]),
        dst=str(row[1]),
        kind=str(row[2]),
        src_file=None if row[3] is None else str(row[3]),
        is_ambiguous=int(row[4]),
        attrs=_attrs(row[5]),
    )


def _chunks(values: Sequence[str]) -> Iterable[Sequence[str]]:
    for start in range(0, len(values), _CHUNK):
        yield values[start : start + _CHUNK]


def _find(index: Index, node_id: str) -> NodeRow | None:
    row = index.connection.execute(
        f"SELECT {_NODE_COLUMNS} FROM nodes WHERE id = ?", (node_id,)
    ).fetchone()
    return None if row is None else _node_row(row)


def _nodes_by_id(index: Index, ids: Sequence[str]) -> dict[str, NodeRow]:
    found: dict[str, NodeRow] = {}
    for chunk in _chunks(ids):
        placeholders = ",".join("?" for _ in chunk)
        rows = index.connection.execute(
            f"SELECT {_NODE_COLUMNS} FROM nodes WHERE id IN ({placeholders})", tuple(chunk)
        )
        for row in rows:
            parsed = _node_row(row)
            found[parsed.id] = parsed
    return found


def node(index: Index, node_id: str) -> NodeRow:
    """The node with that ID, or `UnknownNodeError` naming it (AC-16.2).

    The read behind `query node` and `/api/nodes/{id}`; `slice()` uses it to establish
    that an origin exists before it traverses anything.
    """
    found = _find(index, node_id)
    if found is None:
        raise UnknownNodeError(node_id)
    return found


# ---------------------------------------------------------------------------
# Node search (the §5.2 `/api/nodes?search=` read; AC-26.2)
# ---------------------------------------------------------------------------

#: LIKE's own wildcards, escaped so a search for `a_b` matches `a_b` and not `axb`.
_LIKE_ESCAPE = "\\"

_SEARCH = f"""
    SELECT {_NODE_COLUMNS} FROM nodes
     WHERE id LIKE ? ESCAPE '{_LIKE_ESCAPE}' OR name LIKE ? ESCAPE '{_LIKE_ESCAPE}'
     ORDER BY id
     LIMIT ?
"""


def search_nodes(index: Index, term: str, limit: int) -> list[NodeRow]:
    """Nodes whose id or name contains `term`, by id, at most `limit` of them.

    The read behind `/api/nodes?search=` (§5.2), which is AC-26.2's alternative: with zero
    entry points — EC-9's normal outcome for a library — the viewer offers FR-17's
    slice-from-any-node, and this is how the user names the node. It lives here with the
    other index reads because it speaks the same row vocabulary, and it has no CLI
    counterpart to drift from.

    Matching is substring, case-insensitive over ASCII (SQLite's `LIKE`), and the result is
    ordered by id: a truncated answer is then a stable prefix rather than whatever the
    query plan produced first.

    `limit` carries no default here on purpose: the number §5.2 publishes (50) is the
    *API's*, so it has one definition site, in `viewer.server` — the same discipline
    §8-O2's slice bound gets, pointed the other way because this read has no CLI surface.
    """
    if limit < 1:
        raise ValueError(f"limit must be at least 1, not {limit!r}")
    escaped = (
        term.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
        .replace("%", f"{_LIKE_ESCAPE}%")
        .replace("_", f"{_LIKE_ESCAPE}_")
    )
    pattern = f"%{escaped}%"
    rows = index.connection.execute(_SEARCH, (pattern, pattern, limit))
    return [_node_row(row) for row in rows]


# ---------------------------------------------------------------------------
# Entry points (FR-8; the §5.2 listing)
# ---------------------------------------------------------------------------

#: Every entry node with the target of its `calls` edge, ordered by id (§5.2's "sorted by
#: id"). The subquery takes the lowest target id, so an index that somehow held two edges
#: for one entry still answers deterministically rather than by query plan.
_ENTRY_POINTS = """
    SELECT n.id, n.name, n.file_path, n.start_line, n.attrs,
           (SELECT MIN(e.dst) FROM edges e WHERE e.src = n.id AND e.kind = 'calls')
      FROM nodes n
     WHERE n.kind = 'entry_point'
     ORDER BY n.id
"""


def entry_points(index: Index) -> list[EntryPoint]:
    """Every detected entry point in the index, sorted by id (FR-8, FR-20).

    The read behind `query entry-points` and `/api/entry-points`. Detection itself happened
    during `analyze` (design.md §3.7, D18); this is the index answering afterwards, with no
    engine and no source tree in reach (AC-20.1).

    Zero entry points is an answer, not a failure: EC-9 makes it the *expected* outcome for
    a library, whose entry points are its public API. Every caller says so explicitly rather
    than showing an empty list (AC-26.2's CLI counterpart).
    """
    return [
        EntryPoint(
            id=str(identifier),
            name=str(name),
            detector=str(_attrs(attrs).get(ATTR_DETECTOR, "")),
            target_id=None if target is None else str(target),
            file_path=None if file_path is None else str(file_path),
            start_line=None if start_line is None else int(start_line),
        )
        for identifier, name, file_path, start_line, attrs, target in index.connection.execute(
            _ENTRY_POINTS
        )
    ]


# ---------------------------------------------------------------------------
# Slices (FR-15, FR-16, FR-17; D5)
# ---------------------------------------------------------------------------


def _slice_edges(index: Index, origin: str, direction: str) -> list[EdgeRow]:
    """Every `calls` edge leaving the traversal's reachable set, via one recursive CTE.

    D5's mechanism, and the one query the whole slice rests on. The CTE dedupes by node
    id, which is what makes it terminate on the cyclic call graphs real code produces —
    carrying a depth column instead would make `(id, depth)` the dedup key and a cycle an
    infinite one. Depth is therefore recovered in Python, from these very edges, where a
    cycle costs nothing.
    """
    near, far = ("src", "dst") if direction == FORWARD else ("dst", "src")
    sql = f"""
        WITH RECURSIVE reached(id) AS (
            SELECT ?
            UNION
            SELECT e.{far} FROM edges e JOIN reached r ON e.{near} = r.id
             WHERE e.kind = 'calls'
        )
        SELECT {_EDGE_COLUMNS} FROM edges e JOIN reached r ON e.{near} = r.id
         WHERE e.kind = 'calls'
    """
    return [_edge_row(row) for row in index.connection.execute(sql, (origin,))]


def _depths(origin: str, adjacency: Mapping[str, list[str]]) -> dict[str, int]:
    """Shortest-hop distance from `origin`, over the slice's own adjacency."""
    depths = {origin: 0}
    queue = deque([origin])
    while queue:
        current = queue.popleft()
        for neighbor in adjacency.get(current, ()):
            if neighbor not in depths:
                depths[neighbor] = depths[current] + 1
                queue.append(neighbor)
    return depths


def slice(  # noqa: A001 - design.md §3.9 names this function; the builtin is unused here
    index: Index,
    node_id: str,
    direction: str = FORWARD,
    max_nodes: int = SLICE_MAX_NODES,
) -> SliceResult:
    """The forward or backward call slice from `node_id` (FR-15, FR-16, FR-17).

    Forward follows `calls` edges `src→dst` and answers "what does this reach"; backward
    follows them `dst→src` and answers "what reaches this". The result is the subgraph,
    never the whole graph (AC-15.1): only nodes the traversal actually reached appear, and
    only the edges among them.

    Bounding is visible, never silent (AC-28.2, FR-28). Nodes are admitted in BFS order
    until `max_nodes` is spent; if the traversal reached more, `truncated` is set and
    `frontier` names the reached-but-excluded nodes adjacent to what was kept — the
    boundary a viewer offers to expand. Because the admitted set is a prefix of
    `(depth, id)` order, every excluded node's shortest-path parent sorts earlier and is
    therefore admitted, so a truncated slice always has a non-empty frontier.

    An origin with no edges in the chosen direction is an empty slice — one node, no edges
    — and is returned as a result, not raised as an error (AC-15.2). An unknown ID raises
    `UnknownNodeError` (AC-16.2); a `file` node raises `NotSliceableError` (AC-17.2).
    """
    if direction not in DIRECTIONS:
        raise ValueError(f"unknown direction {direction!r}; expected one of {list(DIRECTIONS)}")
    if max_nodes < 1:
        raise ValueError(f"max_nodes must be at least 1, not {max_nodes!r}")

    origin = node(index, node_id)
    if origin.kind not in SLICEABLE_KINDS:
        raise NotSliceableError(node_id, origin.kind)

    edges = _slice_edges(index, origin.id, direction)
    adjacency: dict[str, list[str]] = {}
    for edge in edges:
        near, far = (edge.src, edge.dst) if direction == FORWARD else (edge.dst, edge.src)
        adjacency.setdefault(near, []).append(far)

    depths = _depths(origin.id, adjacency)
    ordered = sorted(depths, key=lambda identifier: (depths[identifier], identifier))
    admitted = ordered[:max_nodes]
    kept = set(admitted)

    frontier = sorted(
        {
            neighbor
            for identifier in admitted
            for neighbor in adjacency.get(identifier, ())
            if neighbor not in kept
        }
    )
    rows = _nodes_by_id(index, admitted)
    return SliceResult(
        # Every admitted id came from an edge endpoint or from the origin, so a missing
        # row would mean an edge referencing a node the index does not hold — which
        # `validate_fragment()` rejects at the write boundary (AC-23.2). Filtering rather
        # than indexing keeps a corrupt index from crashing a read.
        nodes=[rows[identifier] for identifier in admitted if identifier in rows],
        edges=sorted(
            (edge for edge in edges if edge.src in kept and edge.dst in kept),
            key=lambda edge: (edge.src, edge.dst, edge.kind),
        ),
        truncated=len(ordered) > len(admitted),
        frontier=frontier,
    )


# ---------------------------------------------------------------------------
# Reachability (FR-18; D19)
# ---------------------------------------------------------------------------

_KIND_SET = ",".join(f"'{kind}'" for kind in REACHABLE_KINDS)

#: The BFS half: every node reachable from any entry point over `calls` edges. Entry nodes
#: are seeds, not results — they keep `reachable` NULL, per §4.2's column comment.
#:
#: **The `+` on `e.kind` is load-bearing — do not remove it.** It marks the term
#: non-indexable, which is the only way to keep SQLite from planning the recursive step
#: through `ix_edges_dst (kind=?)`: that index seeks on `kind` alone here, so every
#: iteration rescans *every* `calls` edge in the index instead of seeking the current
#: node's own edges through the `edges` primary key (`src, dst, kind`). The cost is the
#: product, and it only shows up at scale. Measured (task 4.4, pinned pandas benchmark,
#: 135,407 call edges): **297 s** indexable, **0.04 s** with the `+`, marking the same
#: 1,223 nodes — 71 % of that benchmark's whole run time was this one statement.
_MARK_BFS = f"""
    WITH RECURSIVE reached(id) AS (
        SELECT id FROM nodes WHERE kind = 'entry_point'
        UNION
        SELECT e.dst FROM reached r JOIN edges e ON e.src = r.id AND +e.kind = 'calls'
    )
    UPDATE nodes SET reachable = 1
     WHERE kind IN ({_KIND_SET}) AND id IN (SELECT id FROM reached)
"""

#: D19's class derivation: reachable iff it contains a reachable function. A *union* with
#: the BFS (amended 2026-07-23), so a class an entry point targets directly — a Django
#: `X.as_view()` route, a `pkg.mod:Class` console script — keeps the 1 the BFS gave it.
_DERIVE_CLASSES = """
    UPDATE nodes SET reachable = 1
     WHERE kind = 'class' AND reachable = 0 AND EXISTS (
       SELECT 1 FROM edges e JOIN nodes f ON f.id = e.dst
        WHERE e.kind = 'contains' AND e.src = nodes.id
          AND f.kind = 'function' AND f.reachable = 1
     )
"""

#: D19's module derivation, over the owning `file` node's `contains` edges (amended
#: 2026-07-23): a module body has none of its own — §3.5 emits file→defs — so the question
#: is whether something that `contains` this module also contains a reachable function.
#: The container's `file` kind is asserted rather than assumed, so this stays a statement
#: about the graph and not about how IDs happen to be spelled.
#:
#: Phrased as set membership rather than a correlated `EXISTS` on `nodes.id` for one
#: reason, measured (task 4.4, on the pinned Django benchmark): correlated, SQLite drove
#: the subquery from `sibling` and rescanned every `contains` edge once per module node —
#: **15.8 s** of a 39.7 s incremental run, which is most of why FR-30's 30 s bound was
#: missed. Computed once as a set, the same statement over the same index is **0.06 s**
#: and marks exactly the same rows. The semantics below are D19's, unchanged.
_DERIVE_MODULES = """
    UPDATE nodes SET reachable = 1
     WHERE kind = 'module' AND reachable = 0 AND id IN (
       SELECT parent.dst
         FROM edges parent
        WHERE parent.kind = 'contains'
          AND parent.src IN (SELECT id FROM nodes WHERE kind = 'file')
          AND parent.src IN (
                SELECT sibling.src
                  FROM edges sibling
                  JOIN nodes fn ON fn.id = sibling.dst
                 WHERE sibling.kind = 'contains'
                   AND fn.kind = 'function' AND fn.reachable = 1
              )
     )
"""


def reachability(index: Index) -> ReachabilityResult:
    """Compute `reachable` over the whole index and write it (FR-18, D19).

    Four statements inside one transaction, in this order because each reads what the last
    wrote:

    1. **reset** — every `reachable` value is cleared, so a re-run can only widen or narrow
       from the graph as it is *now*, never inherit a mark from a graph that is gone;
    2. **floor** — every `function`, `class` and `module` node starts at 0, which is what
       makes "unreachable" a written answer rather than an absent one;
    3. **BFS** — from every `entry_point` node over `calls` edges (AC-18.1);
    4. **derive** — D19's second pass over `contains` edges, adding the classes and modules
       the edge vocabulary cannot reach by call alone, and leaving what the BFS found.

    Zero entry points is not an error: the passes still run (every function lands at 0) and
    the result carries `no_entry_points`, which the run output and the dead-code report turn
    into AC-18.2's explicit warning. That is EC-9's normal outcome for a library.

    Returns the counts the run summary needs; the authoritative result is in the index.
    """
    connection = index.connection
    with index.transaction():
        connection.execute("UPDATE nodes SET reachable = NULL WHERE reachable IS NOT NULL")
        connection.execute(f"UPDATE nodes SET reachable = 0 WHERE kind IN ({_KIND_SET})")
        connection.execute(_MARK_BFS)
        connection.execute(_DERIVE_CLASSES)
        connection.execute(_DERIVE_MODULES)

    entry_points = connection.execute(
        "SELECT COUNT(*) FROM nodes WHERE kind = 'entry_point'"
    ).fetchone()[0]
    reachable_functions = connection.execute(
        "SELECT COUNT(*) FROM nodes WHERE kind = 'function' AND reachable = 1"
    ).fetchone()[0]
    return ReachabilityResult(
        entry_points=int(entry_points), reachable_functions=int(reachable_functions)
    )


# ---------------------------------------------------------------------------
# Dead code (FR-19)
# ---------------------------------------------------------------------------


def dead_code(index: Index) -> DeadCodeResult:
    """The functions no entry point reaches, grouped by file and paired with the caveat.

    `function` nodes only (D16): module bodies leave this report by construction rather
    than by a filter clause, since no `calls` edge reaches one except an FR-9 entry point.
    External leaves are excluded too — FR-36 leaves their internals deliberately
    unanalyzed, so "unreachable" claims nothing about them, and having no `file_path`
    (AC-37.2) they have no group to belong to.

    Ordering is total and stable: groups by file, functions by `(start_line, id)`. The
    caveat and `no_entry_points_warning` ride on the result (AC-19.2, AC-19.3), so no
    renderer can present the findings without the qualification that makes them honest.
    """
    rows = index.connection.execute(
        "SELECT id, name, file_path, start_line FROM nodes"
        " WHERE kind = 'function' AND is_external = 0 AND reachable = 0"
        " ORDER BY file_path, start_line, id"
    )
    groups: dict[str, list[DeadCodeFunction]] = {}
    for identifier, name, file_path, start_line in rows:
        # A non-external node always carries its path (AC-37.1); the fallback exists so a
        # malformed row is reported under an empty group rather than crashing the report.
        groups.setdefault(str(file_path or ""), []).append(
            DeadCodeFunction(
                id=str(identifier),
                name=str(name),
                start_line=None if start_line is None else int(start_line),
            )
        )
    entry_points = index.connection.execute(
        "SELECT COUNT(*) FROM nodes WHERE kind = 'entry_point'"
    ).fetchone()[0]
    return DeadCodeResult(
        unreachable=[DeadCodeGroup(file=path, functions=groups[path]) for path in sorted(groups)],
        no_entry_points_warning=int(entry_points) == 0,
    )


# ---------------------------------------------------------------------------
# Wire shapes (design.md §5.2, normative)
# ---------------------------------------------------------------------------
#
# One producer per shape, consumed by `cli`'s `--json` and by the viewer's HTTP API. The
# field sets below are §5.2's, literally: a slice edge publishes `src`, `dst` and
# `is_ambiguous` and not the columns behind them, because the API is the contract and the
# schema is an implementation detail (design.md R2 calls the §5.2 surface stable while the
# frontend iterates).


def node_json(row: NodeRow) -> dict[str, Any]:
    """`/api/nodes/{id}`'s shape for one node (§5.2)."""
    return {
        "id": row.id,
        "kind": row.kind,
        "name": row.name,
        "file_path": row.file_path,
        "start_line": row.start_line,
        "end_line": row.end_line,
        "is_external": row.is_external,
        "reachable": row.reachable,
        "attrs": dict(row.attrs),
    }


def nodes_json(rows: Sequence[NodeRow]) -> dict[str, Any]:
    """`/api/nodes?search=`'s shape (§5.2): a list of the same node documents."""
    return {"nodes": [node_json(row) for row in rows]}


def entry_points_json(entries: Sequence[EntryPoint]) -> dict[str, Any]:
    """`/api/entry-points`'s shape (§5.2). Order is `entry_points()`'s: by id."""
    return {
        "entry_points": [
            {
                "id": entry.id,
                "name": entry.name,
                "detector": entry.detector,
                "target_id": entry.target_id,
                "file_path": entry.file_path,
                "start_line": entry.start_line,
            }
            for entry in entries
        ]
    }


def slice_json(result: SliceResult) -> dict[str, Any]:
    """`/api/slice`'s shape (§5.2), including AC-28.2's visible bound."""
    return {
        "nodes": [node_json(row) for row in result.nodes],
        "edges": [
            {"src": edge.src, "dst": edge.dst, "is_ambiguous": edge.is_ambiguous}
            for edge in result.edges
        ],
        "truncated": result.truncated,
        "frontier": list(result.frontier),
    }


def dead_code_json(result: DeadCodeResult) -> dict[str, Any]:
    """`deadcode.json`'s shape minus the volatile `run*` block (D20, §5.3, §5.4).

    D20 fixed this as *the* dead-code payload for both surfaces: recomputed from the index
    via `dead_code()`, never read back from the report file, so a query answers correctly
    against an index whose `<out>/reports/` directory is absent. `format_version` rides
    along because it is part of the shape a consumer must be able to refuse (AC-42.4), and
    the caveat rides along because AC-19.2 admits no rendering without it.
    """
    return {
        "format_version": FORMAT_VERSION,
        "caveat": result.caveat,
        "no_entry_points_warning": result.no_entry_points_warning,
        "unreachable": [group.as_json() for group in result.unreachable],
    }


def error_json(exc: Exception) -> dict[str, Any]:
    """§5.2's error body: `{error: {code, message}}`.

    The message is the exception's own text, which every one of these classes writes to
    name the thing that went wrong and what to do about it (AC-16.2, AC-17.2, AC-20.2,
    AC-39.2) — so the structured form says exactly what the one-line stderr form says.
    """
    return {"error": {"code": error_code(exc), "message": str(exc)}}


__all__ = [
    "ATTR_DETECTOR",
    "BACKWARD",
    "DIRECTIONS",
    "ERROR_CODES",
    "ERROR_INDEX_INCOMPATIBLE",
    "ERROR_INDEX_MISSING",
    "ERROR_NOT_SLICEABLE",
    "ERROR_UNKNOWN_NODE",
    "FORWARD",
    "REACHABLE_KINDS",
    "SLICEABLE_KINDS",
    "SLICE_MAX_NODES",
    "DeadCodeFunction",
    "DeadCodeGroup",
    "DeadCodeResult",
    "EntryPoint",
    "NotSliceableError",
    "QueryError",
    "ReachabilityResult",
    "SliceResult",
    "UnknownNodeError",
    "dead_code",
    "dead_code_json",
    "entry_points",
    "entry_points_json",
    "error_code",
    "error_json",
    "node",
    "node_json",
    "nodes_json",
    "reachability",
    "search_nodes",
    "slice",
    "slice_json",
]
