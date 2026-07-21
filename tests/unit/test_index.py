"""The SQLite store: versioning, validation at the write boundary, canonical writes.

design.md §3.8, §4.2, D3, D12; requirements FR-20 (AC-20.2), FR-21 (AC-21.2), FR-22
(AC-22.1/22.2), FR-23 (AC-23.2), FR-39 (AC-39.1-3), FR-40 (AC-40.3), FR-44, EC-13.
"""

import hashlib
import random
import sqlite3

import pytest

from pastapathfinder.index import (
    IndexIncompatibleError,
    IndexMissingError,
    canonical_edges,
    canonical_nodes,
    full_write,
    open_index,
)
from pastapathfinder.schema import (
    NODE_ID_RE,
    VOLATILE_META_KEYS,
    EdgeRow,
    FileRecord,
    FragmentValidationError,
    GraphFragment,
    NodeRow,
)

META = {
    "tool_version": "0.1.0",
    "engine": "mypy",
    "engine_version": "2.3.0",
    "root_path": "/srv/target",
    "created_at": "2026-07-21T09:00:00+00:00",
    "run_id": "11111111-2222-3333-4444-555555555555",
}


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


EXTERNAL = NodeRow(
    id="python:os.path.join",
    kind="function",
    name="join",
    language="python",
    is_external=1,
)


def sample_fragments() -> list[GraphFragment]:
    """Two analyzed files, an external leaf shared by both, and one skipped file.

    Deliberately exercises the shapes the store has to get right: cross-fragment call
    edges, a `module` node (D16), a duplicate external node (AC-36.5), empty `attrs`
    (AC-21.2), and a skipped file with no nodes at all (FR-6).
    """
    a = GraphFragment(
        file=FileRecord("pkg/a.py", digest("a"), "analyzed"),
        nodes=[
            NodeRow("python:file:pkg/a.py", "file", "a.py", "python", file_path="pkg/a.py"),
            NodeRow(
                "python:pkg.a.<module>",
                "module",
                "<module>",
                "python",
                file_path="pkg/a.py",
                start_line=1,
                end_line=20,
                attrs={"python_role": "module_body"},
            ),
            NodeRow(
                "python:pkg.a.main",
                "function",
                "main",
                "python",
                file_path="pkg/a.py",
                start_line=5,
                end_line=9,
            ),
            EXTERNAL,
        ],
        edges=[
            EdgeRow("python:file:pkg/a.py", "python:pkg.a.main", "contains", src_file="pkg/a.py"),
            EdgeRow(
                "python:file:pkg/a.py", "python:pkg.a.<module>", "contains", src_file="pkg/a.py"
            ),
            EdgeRow("python:file:pkg/a.py", "python:file:pkg/b.py", "imports", src_file="pkg/a.py"),
            EdgeRow(
                "python:pkg.a.main",
                "python:pkg.b.helper",
                "calls",
                src_file="pkg/a.py",
                attrs={"call_sites": [[6, 4], [7, 4]]},
            ),
            EdgeRow(
                "python:pkg.a.main",
                "python:os.path.join",
                "calls",
                src_file="pkg/a.py",
                attrs={"call_sites": [[8, 11]]},
            ),
        ],
    )
    b = GraphFragment(
        file=FileRecord("pkg/b.py", digest("b"), "analyzed"),
        nodes=[
            NodeRow("python:file:pkg/b.py", "file", "b.py", "python", file_path="pkg/b.py"),
            NodeRow(
                "python:pkg.b.helper",
                "function",
                "helper",
                "python",
                file_path="pkg/b.py",
                start_line=1,
                end_line=3,
            ),
            EXTERNAL,  # AC-36.5: the same external symbol reached from two files
        ],
        edges=[
            EdgeRow("python:file:pkg/b.py", "python:pkg.b.helper", "contains", src_file="pkg/b.py"),
            EdgeRow(
                "python:pkg.b.helper",
                "python:os.path.join",
                "calls",
                src_file="pkg/b.py",
                attrs={"call_sites": [[2, 11]]},
            ),
        ],
    )
    broken = GraphFragment(
        file=FileRecord("pkg/broken.py", digest("broken"), "skipped", "parse_error")
    )
    return [a, b, broken]


def shuffled(fragments: list[GraphFragment], seed: int) -> list[GraphFragment]:
    """The same rows in a different insertion order (AC-44.3's ordering effect)."""
    rng = random.Random(seed)
    out = []
    for fragment in fragments:
        nodes, edges = list(fragment.nodes), list(fragment.edges)
        rng.shuffle(nodes)
        rng.shuffle(edges)
        out.append(GraphFragment(file=fragment.file, nodes=nodes, edges=edges))
    rng.shuffle(out)
    return out


def build(path, fragments, meta=None):
    with full_write(path, meta or META) as index:
        index.write_fragments(fragments)
    return path


def rows(path, sql):
    with open_index(path) as index:
        return index.connection.execute(sql).fetchall()


# ---------------------------------------------------------------------------
# Versioning (FR-39)
# ---------------------------------------------------------------------------


def test_written_index_carries_its_schema_version(tmp_path):
    """AC-39.1."""
    path = build(tmp_path / "index.sqlite", sample_fragments())
    with open_index(path) as index:
        meta = index.meta()
    assert meta["schema_version"] == "1"
    for key in ("tool_version", "engine", "engine_version", "root_path", *VOLATILE_META_KEYS):
        assert meta[key] == META[key]


def test_a_future_schema_version_is_refused_by_name(tmp_path):
    """AC-39.2: the error names both the found and the supported version."""
    path = build(tmp_path / "index.sqlite", sample_fragments())
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE meta SET value = '2' WHERE key = 'schema_version'")
    with pytest.raises(IndexIncompatibleError) as raised:
        open_index(path)
    assert raised.value.found == "2"
    assert raised.value.supported == "1"
    assert "'2'" in str(raised.value) and "'1'" in str(raised.value)


def test_a_missing_version_key_is_incompatible_not_current(tmp_path):
    """AC-39.3, first half."""
    path = build(tmp_path / "index.sqlite", sample_fragments())
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM meta WHERE key = 'schema_version'")
    with pytest.raises(IndexIncompatibleError, match="missing"):
        open_index(path)


@pytest.mark.parametrize("content", [b"", b"this is not a database at all", b"SQLite format 3\x00"])
def test_an_unreadable_or_corrupt_index_is_incompatible(tmp_path, content):
    """AC-39.3, second half: a `meta` table we cannot read is never read as current."""
    path = tmp_path / "index.sqlite"
    path.write_bytes(content)
    with pytest.raises(IndexIncompatibleError):
        open_index(path)


def test_a_dropped_meta_table_is_incompatible(tmp_path):
    path = build(tmp_path / "index.sqlite", sample_fragments())
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE meta")
    with pytest.raises(IndexIncompatibleError):
        open_index(path)


def test_a_missing_index_names_the_remedy(tmp_path):
    """AC-20.2 / EC-13: the error identifies the problem and what to do about it."""
    with pytest.raises(IndexMissingError) as raised:
        open_index(tmp_path / "index.sqlite")
    assert "analyze" in str(raised.value)


def test_the_store_refuses_to_stamp_a_version_it_cannot_write(tmp_path):
    with pytest.raises(ValueError, match="refusing to write schema_version"):
        with full_write(tmp_path / "index.sqlite", {**META, "schema_version": "2"}):
            pass
    assert not (tmp_path / "index.sqlite").exists()


def test_required_meta_keys_are_enforced(tmp_path):
    incomplete = {key: value for key, value in META.items() if key != "engine_version"}
    with pytest.raises(ValueError, match="engine_version"):
        with full_write(tmp_path / "index.sqlite", incomplete):
            pass


# ---------------------------------------------------------------------------
# Validation at the write boundary (AC-22.1/22.2, AC-23.2)
# ---------------------------------------------------------------------------


def test_every_written_node_id_matches_the_grammar(tmp_path):
    """AC-22.1."""
    path = build(tmp_path / "index.sqlite", sample_fragments())
    ids = [row[0] for row in rows(path, "SELECT id FROM nodes")]
    assert ids
    assert all(NODE_ID_RE.fullmatch(node_id) for node_id in ids)
    endpoints = {end for row in rows(path, "SELECT src, dst FROM edges") for end in row}
    assert endpoints <= set(ids)


@pytest.mark.parametrize(
    ("offender", "expected"),
    [
        pytest.param(
            NodeRow("pkg.c.orphan", "function", "orphan", "python"),
            "§4.1 grammar",
            id="non-namespaced-id",
        ),
        pytest.param(
            NodeRow("python:pkg.c.orphan", "decorator", "orphan", "python"),
            "unknown node kind",
            id="unknown-kind",
        ),
    ],
)
def test_a_rejected_fragment_stores_nothing(tmp_path, offender, expected):
    """AC-22.2 / AC-23.2: the offending row is named and the index is left untouched."""
    path = build(tmp_path / "index.sqlite", sample_fragments())
    before = rows(path, "SELECT id FROM nodes ORDER BY id")

    bad = GraphFragment(
        file=FileRecord("pkg/c.py", digest("c"), "analyzed"),
        nodes=[
            NodeRow("python:file:pkg/c.py", "file", "c.py", "python", file_path="pkg/c.py"),
            offender,
        ],
    )
    with open_index(path) as index:
        with pytest.raises(FragmentValidationError) as raised:
            index.write_fragments([bad])
    assert expected in str(raised.value)
    assert repr(offender.id) in str(raised.value) or offender.id in str(raised.value)

    assert rows(path, "SELECT id FROM nodes ORDER BY id") == before
    assert rows(path, "SELECT path FROM files WHERE path = 'pkg/c.py'") == []


def test_an_edge_endpoint_unknown_to_fragment_and_index_is_rejected(tmp_path):
    """AC-23.2."""
    path = build(tmp_path / "index.sqlite", sample_fragments())
    dangling = GraphFragment(
        file=FileRecord("pkg/c.py", digest("c"), "analyzed"),
        nodes=[NodeRow("python:pkg.c.f", "function", "f", "python", file_path="pkg/c.py")],
        edges=[EdgeRow("python:pkg.c.f", "python:pkg.nowhere.g", "calls", src_file="pkg/c.py")],
    )
    with open_index(path) as index:
        with pytest.raises(FragmentValidationError, match="python:pkg.nowhere.g"):
            index.write_fragments([dangling])
    assert rows(path, "SELECT src FROM edges WHERE src = 'python:pkg.c.f'") == []


def test_an_edge_may_point_at_a_node_already_in_the_index(tmp_path):
    """The other half of AC-23.2: "in the fragment *or* in the index"."""
    path = build(tmp_path / "index.sqlite", sample_fragments())
    later = GraphFragment(
        file=FileRecord("pkg/c.py", digest("c"), "analyzed"),
        nodes=[NodeRow("python:pkg.c.f", "function", "f", "python", file_path="pkg/c.py")],
        edges=[EdgeRow("python:pkg.c.f", "python:pkg.b.helper", "calls", src_file="pkg/c.py")],
    )
    with open_index(path) as index:
        index.write_fragments([later])
    assert rows(path, "SELECT dst FROM edges WHERE src = 'python:pkg.c.f'") == [
        ("python:pkg.b.helper",)
    ]


def test_identical_duplicate_rows_collapse_but_conflicting_ones_are_rejected():
    """AC-36.5's external node is emitted per caller; two versions of it are a bug."""
    assert canonical_nodes([EXTERNAL, EXTERNAL]) == [EXTERNAL]
    conflicting = NodeRow(EXTERNAL.id, "class", EXTERNAL.name, "python", is_external=1)
    with pytest.raises(FragmentValidationError, match="conflicting node rows"):
        canonical_nodes([EXTERNAL, conflicting])

    edge = EdgeRow("python:pkg.a.main", "python:pkg.b.helper", "calls")
    assert canonical_edges([edge, edge]) == [edge]


# ---------------------------------------------------------------------------
# Generic queries do not depend on language detail (AC-21.2, AC-40.3)
# ---------------------------------------------------------------------------

# A slice-shaped traversal, written here rather than imported: `queries.py` belongs to
# task 3.4. It exists to prove the *store* answers a generic query without consulting
# `attrs` or the ambiguity flag.
_FORWARD_SLICE = """
WITH RECURSIVE reached(id) AS (
    SELECT :start
    UNION
    SELECT edges.dst FROM edges JOIN reached ON edges.src = reached.id
    WHERE edges.kind = 'calls'
)
SELECT id FROM reached ORDER BY id
"""


def test_generic_queries_work_with_empty_attrs_and_an_absent_ambiguity_flag(tmp_path):
    """AC-21.2 / AC-40.3."""
    path = build(tmp_path / "index.sqlite", sample_fragments())
    with open_index(path) as index:
        # An edge inserted without the language-specific columns at all: `attrs` and
        # `is_ambiguous` fall back to their DDL defaults.
        with index.transaction():
            index.connection.execute(
                "INSERT INTO edges (src, dst, kind) VALUES"
                " ('python:pkg.b.helper', 'python:pkg.a.<module>', 'calls')"
            )
        reached = [
            row[0]
            for row in index.connection.execute(_FORWARD_SLICE, {"start": "python:pkg.a.main"})
        ]
        defaults = index.connection.execute(
            "SELECT attrs, is_ambiguous FROM edges"
            " WHERE src = 'python:pkg.b.helper' AND dst = 'python:pkg.a.<module>'"
        ).fetchone()

    assert defaults == ("{}", 0)
    assert reached == [
        "python:os.path.join",
        "python:pkg.a.<module>",
        "python:pkg.a.main",
        "python:pkg.b.helper",
    ]


def test_the_ambiguity_flag_round_trips_per_edge(tmp_path):
    """FR-40: the flag is written at analysis time (AC-40.1) and read back unchanged.

    The shape is FR-14's over-approximation: one call site with two possible targets
    becomes two edges, both flagged, while the unambiguous call beside them stays 0
    (AC-40.2).
    """
    fragments = sample_fragments()
    fragments[1].nodes.append(
        NodeRow(
            "python:pkg.b.helper2",
            "function",
            "helper2",
            "python",
            file_path="pkg/b.py",
            start_line=5,
            end_line=7,
        )
    )
    candidates = [
        EdgeRow(*args, src_file="pkg/a.py", is_ambiguous=1)
        for args in (
            ("python:pkg.a.<module>", "python:pkg.b.helper", "calls"),
            ("python:pkg.a.<module>", "python:pkg.b.helper2", "calls"),
        )
    ]
    fragments[0].edges.extend(candidates)

    path = build(tmp_path / "index.sqlite", fragments)
    assert rows(
        path, "SELECT dst, is_ambiguous FROM edges WHERE kind = 'calls' ORDER BY src, dst"
    ) == [
        ("python:pkg.b.helper", 1),
        ("python:pkg.b.helper2", 1),
        ("python:os.path.join", 0),
        ("python:pkg.b.helper", 0),
        ("python:os.path.join", 0),
    ]


# ---------------------------------------------------------------------------
# Determinism (FR-44)
# ---------------------------------------------------------------------------


def test_insertion_order_does_not_change_the_database_bytes(tmp_path):
    """FR-44/AC-44.3: the same fragment set written in different orders is byte-identical."""
    first = build(tmp_path / "first.sqlite", sample_fragments())
    second = build(tmp_path / "second.sqlite", shuffled(sample_fragments(), seed=7))
    third = build(tmp_path / "third.sqlite", shuffled(sample_fragments(), seed=99))

    assert first.read_bytes() == second.read_bytes()
    assert first.read_bytes() == third.read_bytes()


def test_rows_are_inserted_in_canonical_order(tmp_path):
    """D12: nodes by id, edges by (src, dst, kind), files by path.

    Read back by `rowid`, which is insertion order — asserting on an unordered SELECT
    would prove nothing, since SQLite is free to answer it from a sorted index.
    """
    path = build(tmp_path / "index.sqlite", shuffled(sample_fragments(), seed=3))
    node_ids = [row[0] for row in rows(path, "SELECT id FROM nodes ORDER BY rowid")]
    edges = rows(path, "SELECT src, dst, kind FROM edges ORDER BY rowid")
    files = [row[0] for row in rows(path, "SELECT path FROM files ORDER BY rowid")]
    assert node_ids == sorted(node_ids)
    assert edges == sorted(edges)
    assert files == sorted(files)


def test_only_the_volatile_meta_keys_differ_between_runs(tmp_path):
    """FR-44 + §5.4: the volatile register is exactly `created_at` and `run_id`."""
    later = {**META, "created_at": "2026-07-22T10:30:00+00:00", "run_id": "aaaa-bbbb"}
    first = build(tmp_path / "first.sqlite", sample_fragments())
    second = build(tmp_path / "second.sqlite", sample_fragments(), meta=later)

    with open_index(first) as one, open_index(second) as two:
        differing = {key for key, value in one.meta().items() if two.meta().get(key) != value}
        assert differing == set(VOLATILE_META_KEYS)
        for table in ("SELECT * FROM files", "SELECT * FROM nodes", "SELECT * FROM edges"):
            assert (
                one.connection.execute(table).fetchall() == two.connection.execute(table).fetchall()
            )

    assert first.read_bytes() != second.read_bytes()  # the volatile values really did move


# ---------------------------------------------------------------------------
# Atomicity (design.md §3.8, EC-13)
# ---------------------------------------------------------------------------


def test_a_failed_full_write_publishes_nothing(tmp_path):
    path = tmp_path / "index.sqlite"
    with pytest.raises(RuntimeError):
        with full_write(path, META) as index:
            index.write_fragments(sample_fragments())
            raise RuntimeError("the run fell over")
    assert not path.exists()
    assert not (tmp_path / "index.sqlite.tmp").exists()


def test_a_failed_full_write_leaves_the_previous_index_intact(tmp_path):
    path = build(tmp_path / "index.sqlite", sample_fragments())
    before = path.read_bytes()
    with pytest.raises(FragmentValidationError):
        with full_write(path, META) as index:
            index.write_fragments([GraphFragment(file=FileRecord("pkg/a.py", "nope", "analyzed"))])
    assert path.read_bytes() == before
    assert not (tmp_path / "index.sqlite.tmp").exists()


def test_a_failed_merge_transaction_rolls_back(tmp_path):
    path = build(tmp_path / "index.sqlite", sample_fragments())
    before = rows(path, "SELECT id FROM nodes ORDER BY id")
    with open_index(path) as index:
        with pytest.raises(RuntimeError):
            with index.transaction():
                index.connection.execute("DELETE FROM nodes")
                raise RuntimeError("merge failed halfway")
    assert rows(path, "SELECT id FROM nodes ORDER BY id") == before


def test_nested_transactions_commit_once(tmp_path):
    path = build(tmp_path / "index.sqlite", sample_fragments())
    with open_index(path) as index:
        with index.transaction():
            index.write_files([FileRecord("pkg/c.py", digest("c"), "skipped", "encoding_error")])
            index.set_meta({"metadata_hash": digest("meta")})
        assert index.get_meta("metadata_hash") == digest("meta")
    assert rows(path, "SELECT skip_reason FROM files WHERE path = 'pkg/c.py'") == [
        ("encoding_error",)
    ]


# ---------------------------------------------------------------------------
# Reads the pipeline depends on
# ---------------------------------------------------------------------------


def test_content_hashes_covers_analyzed_and_skipped_files(tmp_path):
    path = build(tmp_path / "index.sqlite", sample_fragments())
    with open_index(path) as index:
        assert index.content_hashes() == {
            "pkg/a.py": digest("a"),
            "pkg/b.py": digest("b"),
            "pkg/broken.py": digest("broken"),
        }
        assert "python:pkg.b.helper" in index.node_ids()


def test_a_read_only_index_refuses_writes(tmp_path):
    path = build(tmp_path / "index.sqlite", sample_fragments())
    with open_index(path, read_only=True) as index:
        assert index.get_meta("engine") == "mypy"
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            index.write_files([FileRecord("pkg/c.py", digest("c"), "analyzed")])
