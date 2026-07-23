"""The single source of the data model: node-ID grammar, DDL, row shapes, validation.

design.md §3.8 (`schema.py`'s responsibility), §4.1 (node-ID grammar, normative),
§4.2 (SQLite DDL, normative), §4.3 (adapter fragment), §5.4 (volatile fields), D4, D12;
requirements FR-20, FR-21 (AC-21.1/21.2), FR-22 (AC-22.1/22.2), FR-23 (AC-23.2),
FR-37 (AC-37.2), FR-39, FR-40 (AC-40.3), FR-44.

Nothing here touches SQLite: `index.py` owns the store, this module owns the shapes it
stores and the rules a row must satisfy before it may be stored. The DDL text is
generated from the vocabulary constants below, so the schema's vocabulary has exactly
one definition site (AC-21.1's inspection target).
"""

from __future__ import annotations

import json
import re
from collections.abc import Container, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Versioning (FR-39)
# ---------------------------------------------------------------------------

#: The schema this build reads and writes. Every reader refuses an index carrying any
#: other value (AC-39.2, enforced in index.py).
SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# The generic vocabulary (FR-21; D4)
# ---------------------------------------------------------------------------

#: design.md §4.2's `nodes.kind` CHECK set. `module` is D16's first-class module-body
#: kind. Every member is a language-independent concept: no Python-specific concept may
#: ever join this tuple (AC-21.1) — language detail rides in `attrs` instead.
NODE_KINDS: tuple[str, ...] = ("file", "module", "function", "class", "entry_point")

#: design.md §4.2's `edges.kind` CHECK set (FR-21).
EDGE_KINDS: tuple[str, ...] = ("calls", "contains", "imports")

#: design.md §4.2's `files.status` CHECK set. Excluded paths are not `files` rows: they
#: are reported by `exclusions.json` and never analyzed (FR-5, FR-7).
FILE_STATUSES: tuple[str, ...] = ("analyzed", "skipped")

#: The reason classes a skipped file may carry (design.md §4.2 `skip_reason`, §3.5).
SKIP_REASONS: tuple[str, ...] = ("parse_error", "encoding_error", "engine_error")

#: The C-10 diagnostic classes (design.md §4.3). A new class is a design change.
DIAG_KINDS: tuple[str, ...] = (
    "unresolved_call",
    "detector_error",
    "probe_failure",
    "symlink_skip",
    "span_missing",
    "gitignore_problem",
    "change_check_failure",
    "unresolved_entry_declaration",
)

# ---------------------------------------------------------------------------
# `meta` keys (design.md §4.2, §5.4)
# ---------------------------------------------------------------------------

#: Written by the store itself; callers never supply it.
META_SCHEMA_VERSION = "schema_version"

#: design.md §4.2's required `meta` keys, minus the one the store owns.
REQUIRED_META_KEYS: tuple[str, ...] = (
    "tool_version",
    "engine",
    "engine_version",
    "root_path",
    "created_at",
    "run_id",
)

#: design.md §5.4's volatile register, index half — the *only* index content permitted
#: to differ between two runs over identical input (FR-44). The register's report half
#: lives with the report writers; the comparator (task 4.3) consumes both.
VOLATILE_META_KEYS: tuple[str, ...] = ("created_at", "run_id")

#: Optional key added by D18's change gate (task 4.1); not required to write an index.
META_METADATA_HASH = "metadata_hash"

# ---------------------------------------------------------------------------
# Node-ID grammar (design.md §4.1, normative; FR-22)
# ---------------------------------------------------------------------------

LANGUAGE_PYTHON = "python"

#: v1 ships one language (FR-22, requirements §6 item 6). Adding one changes the grammar.
LANGUAGES: tuple[str, ...] = (LANGUAGE_PYTHON,)

#: The `detector` production of §4.1.
DETECTORS: tuple[str, ...] = (
    "main_block",
    "console_script",
    "route_flask_fastapi",
    "route_django",
)

# mod_seg := any non-empty run of characters other than the grammar's own delimiters and
# the path separators the §4.1 derivation has already consumed (amended 2026-07-22, D22).
# A module segment is a *path* segment, not a Python identifier: `0001_initial.py` and
# `my-app/` are ordinary inputs, and 23 files of the pinned Django benchmark are the
# former. `mod_seg` subsumes the `segment` production (`identifier | "<module>" |
# "<lambda#" N ">"`), so `qualname := module { "." segment }` collapses to a run of module
# segments — D22 records that consequence, and `normalize.py` remains the only producer of
# the bracketed forms.
_MODULE_SEGMENT = r"[^.:@/\\\x00-\x1f]+"
_QUALNAME = rf"{_MODULE_SEGMENT}(?:\.{_MODULE_SEGMENT})*"
# relpath is POSIX-style and root-relative (§4.1): no leading "/", no "." or ".."
# segment, no backslash, no control characters.
_PATH_SEGMENT = r"(?!\.\.?(?:/|\Z))[^/\\\x00-\x1f]+"
_RELPATH = rf"{_PATH_SEGMENT}(?:/{_PATH_SEGMENT})*"

NODE_ID_PATTERN = (
    rf"(?:{'|'.join(LANGUAGES)}):(?:"
    rf"file:{_RELPATH}"
    rf"|entry:(?:{'|'.join(DETECTORS)}):{_QUALNAME}@\d+"
    rf"|{_QUALNAME}(?:@\d+)?"
    rf")"
)

#: The AC-22.1/AC-22.2 gate. Always applied with `fullmatch`.
NODE_ID_RE = re.compile(NODE_ID_PATTERN)


def is_valid_node_id(node_id: object) -> bool:
    """True when `node_id` conforms to the §4.1 grammar (FR-22)."""
    return isinstance(node_id, str) and NODE_ID_RE.fullmatch(node_id) is not None


def node_id_language(node_id: str) -> str | None:
    """The language namespace of an ID, or None when it carries none (AC-22.2)."""
    language, separator, _ = node_id.partition(":")
    return language if separator and language in LANGUAGES else None


# ---------------------------------------------------------------------------
# FR-19's mandatory caveat
# ---------------------------------------------------------------------------

#: FR-19/AC-19.2: every rendering of dead-code findings — report, stdout, viewer —
#: carries this string verbatim. It lives here because `queries.dead_code()` pairs it
#: with every result it returns (design.md §3.9), so no renderer can forget it.
DEADCODE_CAVEAT = (
    "Approximate result. Reachability is computed statically, and Python is dynamic: "
    "calls made through getattr, registries, reflection, framework dispatch, or from an "
    "entry point this tool does not detect are invisible to the analysis. Code listed "
    "here may still be reached at run time. Treat every entry as a candidate for review, "
    "never as proof that the code is unused."
)

# ---------------------------------------------------------------------------
# SQLite DDL (design.md §4.2, normative)
# ---------------------------------------------------------------------------


def _sql_set(values: Iterable[str]) -> str:
    """Render a CHECK set literal, e.g. `'file','module'`."""
    return ",".join(f"'{value}'" for value in values)


# The statements below reproduce §4.2; the CHECK sets are interpolated from the
# vocabulary constants above so the two cannot drift. Referential integrity between
# `edges` and `nodes` is enforced by `validate_fragment()` rather than by SQLite foreign
# keys: AC-23.2 requires the *fragment* to be rejected with an error naming the offending
# row, which a constraint violation at INSERT time cannot express.
DDL: tuple[str, ...] = (
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    f"""CREATE TABLE files (
  path TEXT PRIMARY KEY,
  content_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ({_sql_set(FILE_STATUSES)})),
  skip_reason TEXT
)""",
    f"""CREATE TABLE nodes (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ({_sql_set(NODE_KINDS)})),
  name TEXT NOT NULL,
  language TEXT NOT NULL,
  file_path TEXT,
  start_line INTEGER,
  end_line INTEGER,
  is_external INTEGER NOT NULL DEFAULT 0,
  reachable INTEGER,
  attrs TEXT NOT NULL DEFAULT '{{}}'
)""",
    f"""CREATE TABLE edges (
  src TEXT NOT NULL REFERENCES nodes(id),
  dst TEXT NOT NULL REFERENCES nodes(id),
  kind TEXT NOT NULL CHECK (kind IN ({_sql_set(EDGE_KINDS)})),
  src_file TEXT,
  is_ambiguous INTEGER NOT NULL DEFAULT 0,
  attrs TEXT NOT NULL DEFAULT '{{}}',
  PRIMARY KEY (src, dst, kind)
)""",
    "CREATE INDEX ix_edges_dst ON edges(kind, dst)",
    "CREATE INDEX ix_edges_srcfile ON edges(src_file)",
)

# ---------------------------------------------------------------------------
# Row shapes (design.md §4.2 columns, §4.3 fragment)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FileRecord:
    """One row of `files`: an analyzed or skipped source file (FR-7)."""

    path: str  # root-relative POSIX path
    content_hash: str  # sha256 hex of the file bytes as read
    status: str  # FILE_STATUSES
    skip_reason: str | None = None  # SKIP_REASONS; None unless status == "skipped"


@dataclass(frozen=True, slots=True)
class NodeRow:
    """One row of `nodes`. Language-specific detail belongs in `attrs` (FR-21, D4)."""

    id: str
    kind: str  # NODE_KINDS
    name: str
    language: str  # LANGUAGES
    file_path: str | None = None  # None on external nodes (AC-37.2)
    start_line: int | None = None  # None when the span is unknown (AC-37.3)
    end_line: int | None = None
    is_external: int = 0
    reachable: int | None = None  # written by queries.reachability() (task 3.4)
    attrs: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EdgeRow:
    """One row of `edges`. Multiple call sites collapse into one edge (§4.2)."""

    src: str
    dst: str
    kind: str  # EDGE_KINDS
    src_file: str | None = None  # caller file, for D6 rule 3's eviction
    is_ambiguous: int = 0  # FR-40: 1 on every over-approximated candidate
    attrs: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GraphFragment:
    """One source file's contribution to the graph (design.md §3.4, §4.3)."""

    file: FileRecord
    nodes: list[NodeRow] = field(default_factory=list)
    edges: list[EdgeRow] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class SkipRecord:
    """A discovered, non-excluded file that was not successfully analyzed (FR-6)."""

    path: str
    reason: str  # SKIP_REASONS
    detail: str = ""  # AC-7.2's human-readable reason

    def __post_init__(self) -> None:
        if self.reason not in SKIP_REASONS:
            raise ValueError(
                f"unknown skip reason {self.reason!r} for {self.path!r}; "
                f"design.md §4.2 defines {list(SKIP_REASONS)}"
            )


@dataclass(frozen=True, slots=True)
class Diag:
    """One non-fatal anomaly for the run's diagnostics (requirements §4 conventions)."""

    kind: str  # DIAG_KINDS
    path: str | None = None
    line: int | None = None
    col: int | None = None
    message: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in DIAG_KINDS:
            raise ValueError(
                f"unknown diagnostic kind {self.kind!r}; design.md §4.3 defines {list(DIAG_KINDS)}"
            )


# ---------------------------------------------------------------------------
# Canonical JSON (D12)
# ---------------------------------------------------------------------------


def attrs_json(attrs: Mapping[str, Any]) -> str:
    """Serialize an `attrs` mapping canonically: sorted keys, no incidental whitespace.

    D12's determinism discipline: two runs producing equal `attrs` must produce equal
    bytes. Ordering *within* an attrs value (e.g. `call_sites`) is the producer's
    responsibility — this function preserves list order, it does not impose one.
    """
    return json.dumps(attrs, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# ---------------------------------------------------------------------------
# Fragment validation (design.md §3.8; AC-22.2, AC-23.2)
# ---------------------------------------------------------------------------


class FragmentValidationError(ValueError):
    """A fragment does not conform to the schema; nothing from it is stored.

    AC-22.2 and AC-23.2 both require the rejection to name the offending row, so every
    message carries that row's repr alongside the rule it broke.
    """


class _EitherContainer(Container[str]):
    """Membership in either of two containers, without materializing their union."""

    __slots__ = ("_first", "_second")

    def __init__(self, first: Container[str], second: Container[str]) -> None:
        self._first = first
        self._second = second

    def __contains__(self, item: object) -> bool:
        return item in self._first or item in self._second


def _reject(context: str, reason: str, row: object) -> None:
    raise FragmentValidationError(f"{context}: {reason}: {row!r}")


def _check_relpath(value: object, field_name: str, context: str, row: object) -> None:
    if not isinstance(value, str) or not value:
        _reject(context, f"{field_name} must be a non-empty string", row)
        return
    if value.startswith("/") or "\\" in value:
        _reject(context, f"{field_name} must be a root-relative POSIX path ({value!r})", row)
    if any(part in ("", ".", "..") for part in value.split("/")):
        _reject(context, f"{field_name} contains a non-normalized segment ({value!r})", row)


def _check_attrs(attrs: object, context: str, row: object) -> None:
    if not isinstance(attrs, Mapping):
        _reject(context, "attrs must be a mapping", row)
        return
    if any(not isinstance(key, str) for key in attrs):
        _reject(context, "attrs keys must be strings", row)
    try:
        attrs_json(attrs)
    except (TypeError, ValueError) as exc:
        _reject(context, f"attrs is not JSON-serializable ({exc})", row)


def _check_flag(value: object, field_name: str, context: str, row: object) -> None:
    if isinstance(value, bool) or value not in (0, 1):
        _reject(context, f"{field_name} must be 0 or 1", row)


def _check_line(value: object, field_name: str, context: str, row: object) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        _reject(context, f"{field_name} must be a positive line number or None", row)


_HEX64_RE = re.compile(r"[0-9a-f]{64}")


def _validate_file(record: object, context: str) -> None:
    if not isinstance(record, FileRecord):
        _reject(context, "fragment.file must be a FileRecord", record)
        return
    _check_relpath(record.path, "path", context, record)
    if not _HEX64_RE.fullmatch(record.content_hash or ""):
        _reject(context, "content_hash must be a lowercase sha256 hex digest", record)
    if record.status not in FILE_STATUSES:
        _reject(context, f"unknown file status (expected one of {list(FILE_STATUSES)})", record)
    if record.status == "skipped":
        if record.skip_reason not in SKIP_REASONS:
            _reject(context, f"skip_reason must be one of {list(SKIP_REASONS)}", record)
    elif record.skip_reason is not None:
        _reject(context, "skip_reason must be None on an analyzed file", record)


def _validate_node(node: object, context: str) -> None:
    if not isinstance(node, NodeRow):
        _reject(context, "node must be a NodeRow", node)
        return
    if not is_valid_node_id(node.id):
        # AC-22.2: a non-namespaced (or otherwise malformed) ID is rejected, not stored.
        _reject(context, "node id does not match the §4.1 grammar", node)
    if node.kind not in NODE_KINDS:
        # AC-23.2: an unknown kind is a validation error naming the row.
        _reject(context, f"unknown node kind (expected one of {list(NODE_KINDS)})", node)
    if not isinstance(node.name, str) or not node.name:
        _reject(context, "name must be a non-empty string", node)
    if node.language not in LANGUAGES:
        _reject(context, f"unknown language (expected one of {list(LANGUAGES)})", node)
    if node.language != node_id_language(node.id):
        _reject(context, "language disagrees with the id's namespace", node)
    _check_flag(node.is_external, "is_external", context, node)
    if isinstance(node.reachable, bool) or node.reachable not in (None, 0, 1):
        _reject(context, "reachable must be 0, 1, or None", node)
    _check_line(node.start_line, "start_line", context, node)
    _check_line(node.end_line, "end_line", context, node)
    if (
        node.start_line is not None
        and node.end_line is not None
        and node.end_line < node.start_line
    ):
        _reject(context, "end_line precedes start_line", node)
    if node.is_external:
        # AC-37.2: external nodes carry no source location — FR-36 forbids analyzing
        # their internals, so there is nothing to point at.
        if node.file_path is not None or node.start_line is not None or node.end_line is not None:
            _reject(context, "external nodes carry no source location (AC-37.2)", node)
    elif node.file_path is not None:
        _check_relpath(node.file_path, "file_path", context, node)
    _check_attrs(node.attrs, context, node)


def _validate_edge(edge: object, known_ids: Container[str], context: str) -> None:
    if not isinstance(edge, EdgeRow):
        _reject(context, "edge must be an EdgeRow", edge)
        return
    for field_name, value in (("src", edge.src), ("dst", edge.dst)):
        if not is_valid_node_id(value):
            _reject(context, f"edge {field_name} does not match the §4.1 grammar", edge)
    if edge.kind not in EDGE_KINDS:
        _reject(context, f"unknown edge kind (expected one of {list(EDGE_KINDS)})", edge)
    _check_flag(edge.is_ambiguous, "is_ambiguous", context, edge)
    if edge.src_file is not None:
        _check_relpath(edge.src_file, "src_file", context, edge)
    _check_attrs(edge.attrs, context, edge)
    for field_name, value in (("src", edge.src), ("dst", edge.dst)):
        if value not in known_ids:
            # AC-23.2: an endpoint present neither in the fragment nor in the index.
            _reject(context, f"edge {field_name} {value!r} is not a known node", edge)


def validate_rows(
    nodes: Iterable[object],
    edges: Iterable[object],
    known_ids: Container[str] = frozenset(),
    context: str = "rows",
) -> None:
    """Reject any node or edge that does not conform to §4.2, naming the offending row.

    The row-level half of `validate_fragment()`, exposed because not every set of rows
    belongs to one file: the entry-point nodes a detector pass emits span the analyzed set
    (D18), and a project-level detector's nodes belong to no source file at all. They face
    the same gate all the same — an entry node with a malformed ID, or an edge to a target
    that vanished since the last run, is rejected here rather than stored (AC-22.2,
    AC-23.2).
    """
    node_rows = list(nodes)
    for node in node_rows:
        _validate_node(node, context)
    local_ids = {node.id for node in node_rows if isinstance(node, NodeRow)}
    reachable_ids = _EitherContainer(local_ids, known_ids)
    for edge in edges:
        _validate_edge(edge, reachable_ids, context)


def validate_fragment(fragment: object, known_ids: Container[str] = frozenset()) -> None:
    """Reject a fragment that does not conform to §4.2, naming the offending row.

    `known_ids` is the set of node IDs an edge may point at *in addition to* the
    fragment's own nodes — in practice the index's existing node IDs plus the IDs of the
    other fragments written in the same batch (AC-23.2's "neither in the fragment nor in
    the index").

    Raises `FragmentValidationError` on the first offending row; the caller stores
    nothing (AC-22.2).
    """
    if not isinstance(fragment, GraphFragment):
        raise FragmentValidationError(f"not a GraphFragment: {fragment!r}")
    path = fragment.file.path if isinstance(fragment.file, FileRecord) else "<unknown file>"
    context = f"fragment for {path!r}"
    _validate_file(fragment.file, context)
    validate_rows(fragment.nodes, fragment.edges, known_ids, context)
