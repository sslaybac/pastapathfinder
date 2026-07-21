"""The SQLite index store: create/open, version enforcement, canonical writes.

design.md §3.8 (`index.py`'s responsibility), §4.2 (the DDL it materializes), D3 (SQLite,
single file), D12 (canonical sort at the write boundary); requirements FR-20, FR-22
(AC-22.1/22.2), FR-23 (AC-23.2), FR-39 (AC-39.1-3), FR-44 (write discipline), EC-13.

Two write shapes, per §3.8:

* **Full runs** build `index.sqlite.tmp` and atomically rename it over `index.sqlite`, so
  a failed run never leaves a half-written index in place (`full_write`).
* **Incremental merges** run inside one transaction against the existing file
  (`open_index` + `Index.transaction`).

Both pass through the same canonical-sort layer: rows are sorted by primary key before
insertion (nodes by `id`, edges by `(src, dst, kind)`, files by `path`, meta by `key`)
and `attrs` is serialized with sorted keys. Two runs producing the same rows in different
orders therefore produce the same database bytes (FR-44/AC-44.3).
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pastapathfinder.schema import (
    DDL,
    META_SCHEMA_VERSION,
    REQUIRED_META_KEYS,
    SCHEMA_VERSION,
    EdgeRow,
    FileRecord,
    FragmentValidationError,
    GraphFragment,
    NodeRow,
    attrs_json,
    validate_fragment,
)

#: The index's fixed name inside the output directory (design.md §5.1's `--out` tree).
INDEX_FILENAME = "index.sqlite"

#: The atomic-write staging suffix of design.md §3.8: `index.sqlite.tmp`.
TEMP_SUFFIX = ".tmp"

#: Pinned so that two indexes built from identical input are byte-identical even across
#: machines whose SQLite builds default differently (FR-44).
PAGE_SIZE = 4096


class IndexStoreError(Exception):
    """Base class for every failure to open or use an index (AC-20.2, EC-13)."""


class IndexMissingError(IndexStoreError):
    """No index exists at the requested location (AC-20.2, EC-13)."""


class IndexIncompatibleError(IndexStoreError):
    """The index's schema version is unsupported, missing, or unreadable.

    AC-39.2 requires the refusal to name the found and the supported version; AC-39.3
    requires a missing or unreadable identifier to land here too, never to be read as
    current.
    """

    def __init__(self, path: Path, found: str | None, detail: str = "") -> None:
        self.path = path
        self.found = found
        self.supported = str(SCHEMA_VERSION)
        found_text = f"{found!r}" if found is not None else "no schema_version value"
        message = (
            f"index {path}: found {found_text}, supported version {self.supported!r}; "
            f"re-run `pastapathfinder analyze` to rebuild it"
        )
        if detail:
            message = f"{message} ({detail})"
        super().__init__(message)


# ---------------------------------------------------------------------------
# Canonical-sort layer (D12)
# ---------------------------------------------------------------------------


def _canonical[RowT](rows: Iterable[RowT], key: Callable[[RowT], Any], label: str) -> list[RowT]:
    """Sort `rows` by primary key and collapse identical duplicates.

    Duplicates are expected: an external leaf node (D15) is emitted by every fragment
    that calls into it, and AC-36.5 wants exactly one node in the index. Duplicates that
    *differ* are a producer bug — storing either one would make the result depend on
    insertion order, which is what FR-44 forbids — so they are rejected by name.
    """
    seen: dict[Any, RowT] = {}
    for row in rows:
        row_key = key(row)
        previous = seen.get(row_key)
        if previous is None:
            seen[row_key] = row
        elif previous != row:
            raise FragmentValidationError(
                f"conflicting {label} rows for the same key {row_key!r}: {previous!r} vs {row!r}"
            )
    return [seen[row_key] for row_key in sorted(seen)]


def canonical_files(records: Iterable[FileRecord]) -> list[FileRecord]:
    """`files` rows in canonical order: by path."""
    return _canonical(records, key=lambda record: record.path, label="file")


def canonical_nodes(nodes: Iterable[NodeRow]) -> list[NodeRow]:
    """`nodes` rows in canonical order: by id (D12)."""
    return _canonical(nodes, key=lambda node: node.id, label="node")


def canonical_edges(edges: Iterable[EdgeRow]) -> list[EdgeRow]:
    """`edges` rows in canonical order: by (src, dst, kind) (D12)."""
    return _canonical(edges, key=lambda edge: (edge.src, edge.dst, edge.kind), label="edge")


_INSERT_FILE = (
    "INSERT OR REPLACE INTO files (path, content_hash, status, skip_reason) VALUES (?, ?, ?, ?)"
)
_INSERT_NODE = (
    "INSERT OR REPLACE INTO nodes"
    " (id, kind, name, language, file_path, start_line, end_line, is_external, reachable, attrs)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_INSERT_EDGE = (
    "INSERT OR REPLACE INTO edges (src, dst, kind, src_file, is_ambiguous, attrs)"
    " VALUES (?, ?, ?, ?, ?, ?)"
)
_INSERT_META = "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)"


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


class Index:
    """An open SQLite index. Obtain one from `open_index()` or `full_write()`."""

    def __init__(self, connection: sqlite3.Connection, path: Path) -> None:
        self._connection = connection
        self._path = path
        self._depth = 0

    # -- lifecycle ---------------------------------------------------------

    @property
    def path(self) -> Path:
        """The database file this store is attached to."""
        return self._path

    @property
    def connection(self) -> sqlite3.Connection:
        """The underlying connection, for components that issue their own SQL.

        `queries.py` runs design.md D5's recursive CTEs through this; it is deliberately
        the only read seam, so every *write* still goes through the canonical layer above.
        """
        return self._connection

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Index:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Run a block as one transaction; nested uses join the outermost one.

        The connection is in autocommit mode, so transaction boundaries are explicit:
        one BEGIN..COMMIT per write keeps the number of write transactions — and
        therefore the database bytes — identical between two equivalent runs (FR-44).
        """
        if self._depth:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return

        self._connection.execute("BEGIN IMMEDIATE")
        self._depth = 1
        try:
            yield
        except BaseException:
            self._depth = 0
            self._connection.execute("ROLLBACK")
            raise
        self._depth = 0
        self._connection.execute("COMMIT")

    # -- meta --------------------------------------------------------------

    def meta(self) -> dict[str, str]:
        """Every `meta` key/value pair (design.md §4.2)."""
        rows = self._connection.execute("SELECT key, value FROM meta")
        return {str(key): str(value) for key, value in rows}

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self._connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return default if row is None else str(row[0])

    def set_meta(self, values: Mapping[str, str]) -> None:
        """Write `meta` rows, canonically ordered by key.

        `schema_version` belongs to the store (FR-39): it is written at creation and may
        not be overwritten with a value this build does not support.
        """
        rows = []
        for key, value in values.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValueError(f"meta keys and values must be strings: {key!r} = {value!r}")
            if key == META_SCHEMA_VERSION and value != str(SCHEMA_VERSION):
                raise ValueError(
                    f"refusing to write {META_SCHEMA_VERSION}={value!r}; "
                    f"this build writes {str(SCHEMA_VERSION)!r}"
                )
            rows.append((key, value))
        with self.transaction():
            self._connection.executemany(_INSERT_META, sorted(rows))

    # -- reads used by the pipeline ---------------------------------------

    def node_ids(self) -> set[str]:
        """Every node ID currently in the index (AC-23.2's "or in the index" half)."""
        return {str(row[0]) for row in self._connection.execute("SELECT id FROM nodes")}

    def content_hashes(self) -> dict[str, str]:
        """`path -> sha256` for every recorded file — FR-24's gate and FR-38's check."""
        rows = self._connection.execute("SELECT path, content_hash FROM files")
        return {str(path): str(content_hash) for path, content_hash in rows}

    # -- writes ------------------------------------------------------------

    def write_files(self, records: Iterable[FileRecord]) -> None:
        """Write `files` rows (analyzed or skipped) in canonical order."""
        rows = [
            (record.path, record.content_hash, record.status, record.skip_reason)
            for record in canonical_files(records)
        ]
        with self.transaction():
            self._connection.executemany(_INSERT_FILE, rows)

    def write_fragments(self, fragments: Iterable[GraphFragment]) -> None:
        """Validate a batch of fragments, then write it canonically.

        Every fragment is validated *before* anything is written, so a rejected batch
        stores nothing (AC-22.2, AC-23.2). Edges may point at nodes contributed by any
        fragment in the batch or already present in the index; call this once per batch
        rather than once per file, since each call reads the index's node IDs.
        """
        batch = list(fragments)
        batch_ids = {
            node.id for fragment in batch for node in fragment.nodes if isinstance(node, NodeRow)
        }
        known_ids = batch_ids | self.node_ids()
        for fragment in batch:
            validate_fragment(fragment, known_ids)

        files = canonical_files(fragment.file for fragment in batch)
        nodes = canonical_nodes(node for fragment in batch for node in fragment.nodes)
        edges = canonical_edges(edge for fragment in batch for edge in fragment.edges)

        with self.transaction():
            self._connection.executemany(
                _INSERT_FILE,
                [(f.path, f.content_hash, f.status, f.skip_reason) for f in files],
            )
            self._connection.executemany(
                _INSERT_NODE,
                [
                    (
                        node.id,
                        node.kind,
                        node.name,
                        node.language,
                        node.file_path,
                        node.start_line,
                        node.end_line,
                        node.is_external,
                        node.reachable,
                        attrs_json(node.attrs),
                    )
                    for node in nodes
                ],
            )
            self._connection.executemany(
                _INSERT_EDGE,
                [
                    (
                        edge.src,
                        edge.dst,
                        edge.kind,
                        edge.src_file,
                        edge.is_ambiguous,
                        attrs_json(edge.attrs),
                    )
                    for edge in edges
                ],
            )


# ---------------------------------------------------------------------------
# Opening and creating
# ---------------------------------------------------------------------------


def _connect(path: Path, *, read_only: bool) -> sqlite3.Connection:
    if read_only:
        return sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, isolation_level=None)
    return sqlite3.connect(path, isolation_level=None)


def _enforce_schema_version(connection: sqlite3.Connection, path: Path) -> None:
    """Refuse any index this build does not support (FR-39).

    AC-39.2 covers a version we do not know; AC-39.3 covers a missing key and an
    unreadable or corrupt `meta` table. All three land on `IndexIncompatibleError`: an
    index whose version cannot be established is never treated as current.
    """
    try:
        row = connection.execute(
            "SELECT value FROM meta WHERE key = ?", (META_SCHEMA_VERSION,)
        ).fetchone()
    except sqlite3.DatabaseError as exc:
        raise IndexIncompatibleError(path, None, f"meta table unreadable: {exc}") from exc
    if row is None:
        raise IndexIncompatibleError(path, None, "meta.schema_version is missing")
    found = str(row[0])
    if found != str(SCHEMA_VERSION):
        raise IndexIncompatibleError(path, found)


def open_index(path: Path | str, *, read_only: bool = False) -> Index:
    """Open an existing index, refusing anything this build cannot read.

    Raises `IndexMissingError` when there is no index at `path` (AC-20.2) and
    `IndexIncompatibleError` when its schema version is unsupported, missing, or
    unreadable (AC-39.2, AC-39.3, EC-13).
    """
    path = Path(path)
    if not path.is_file():
        raise IndexMissingError(
            f"no index at {path}; run `pastapathfinder analyze <root>` to create one"
        )
    connection = _connect(path, read_only=read_only)
    try:
        _enforce_schema_version(connection, path)
    except BaseException:
        connection.close()
        raise
    return Index(connection, path)


def _create(path: Path, meta: Mapping[str, str]) -> Index:
    """Create an empty index at `path` and stamp its `meta` (FR-39/AC-39.1)."""
    connection = _connect(path, read_only=False)
    # Pinned before the first table so the file is laid out identically everywhere; both
    # pragmas must run outside a transaction.
    connection.execute(f"PRAGMA page_size = {PAGE_SIZE}")
    connection.execute("PRAGMA encoding = 'UTF-8'")
    connection.execute("PRAGMA journal_mode = DELETE")
    index = Index(connection, path)
    with index.transaction():
        for statement in DDL:
            connection.execute(statement)
        index.set_meta({**meta, META_SCHEMA_VERSION: str(SCHEMA_VERSION)})
    return index


def _validate_meta(meta: Mapping[str, str]) -> None:
    missing = [key for key in REQUIRED_META_KEYS if not meta.get(key)]
    if missing:
        raise ValueError(f"missing required meta keys (design.md §4.2): {missing}")
    supplied = meta.get(META_SCHEMA_VERSION)
    if supplied is not None and supplied != str(SCHEMA_VERSION):
        raise ValueError(
            f"refusing to write {META_SCHEMA_VERSION}={supplied!r}; "
            f"this build writes {str(SCHEMA_VERSION)!r}"
        )


@contextmanager
def full_write(path: Path | str, meta: Mapping[str, str]) -> Iterator[Index]:
    """Build a complete index atomically (design.md §3.8).

    Writes go to `<path>.tmp`; the staging file is renamed over `<path>` only once the
    block completes. An exception leaves the staging file removed and any previous index
    exactly as it was, so a failed run never publishes a partial index (EC-13).
    """
    path = Path(path)
    _validate_meta(meta)
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(path.name + TEMP_SUFFIX)
    staging.unlink(missing_ok=True)

    index = _create(staging, meta)
    try:
        yield index
    except BaseException:
        index.close()
        staging.unlink(missing_ok=True)
        raise
    index.close()
    os.replace(staging, path)
